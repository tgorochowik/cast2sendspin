"""DIAL bridge media worker: YouTube extraction -> PCM -> MA Sendspin source.

stdin/stdout carry bounded JSON RPC messages only. Credentials and raw upstream
errors are never logged. The caller owns the user-facing YouTube queue.
"""
import asyncio
import contextlib
import json
import logging
import math
import os
import re
import signal
import sys
from array import array
from pathlib import Path

import aiohttp
import yt_dlp
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.types import AudioCodec

from ma import MA
from app_config import load_config
from routing import ManualSession, SessionRouter, owns_source, validate_target
from source_client import connect_source
from sender_resolver import SenderResolver

FRAME_BYTES = 4800  # 25 ms, 48 kHz, stereo s16le
SILENCE = bytes(FRAME_BYTES)


def apply_input_volume(pcm, volume):
    """Manual-mode volume changes the input, never an arbitrary MA speaker."""
    level = volume["level"]
    if volume["muted"] or level == 0:
        return bytes(len(pcm))
    if level == 100:
        return pcm
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    gain = (level / 100) ** 1.5
    for index, sample in enumerate(samples):
        samples[index] = max(-32768, min(32767, round(sample * gain)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


class Quiet:
    def debug(self, *_): pass
    def warning(self, *_): pass
    def error(self, *_): pass


def resolve(video_id):
    """Explicit opt-in anonymous fallback, never the default account resolver."""
    with yt_dlp.YoutubeDL({
        "quiet": True, "no_warnings": True, "logger": Quiet(),
        "format": "bestaudio/worst[acodec!=none]/best", "noplaylist": True,
        "js_runtimes": {"node": {}}, "cachedir": ".state/yt-cache",
        "socket_timeout": 15, "retries": 2,
        # This client currently returns usable HLS without the regular web
        # client's PO-token requirement. Prefer an audio-only rendition, else
        # the smallest combined rendition; FFmpeg discards video.
        "extractor_args": {"youtube": {"player_client": ["web_embedded"]}},
    }) as ydl:
        result = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    return {k: result.get(k) for k in ("url", "duration", "title", "http_headers")}


class Worker:
    def __init__(self, ma, config):
        self.ma, self.config = ma, config
        self.manual = config.get("routing_mode") == "manual"
        self.decoder_pending = False
        self.client = None
        self.router = None
        self.capture_task = None
        self.source_epoch = 0
        self.decoder = None
        self.info = None
        self.video_id = None
        self.position = 0.0
        self.duration = 0.0
        self.paused = False
        self.volume = {"level": 0, "muted": False}
        self.generation = 0
        self.streaming = asyncio.Event()
        self.lock = asyncio.Lock()
        self.decoder_read_lock = asyncio.Lock()
        self.shutting_down = False
        self.monitor_task = None
        self.capture_failure = None
        self.resolution_task = None
        self.resolver = SenderResolver(lambda **fields: emit("RESOLVER_TIMING", **fields))
        self.resolver_warmup_task = None

    def invalidate(self):
        self.generation += 1
        if self.resolution_task and not self.resolution_task.done():
            self.resolution_task.cancel()

    async def resolve_media(self, video_id, context):
        try:
            return await self.resolver.resolve(video_id, context)
        except Exception:
            if self.config.get("allow_anonymous_fallback") is not True:
                emit("RESOLUTION_FAILED", reason="Sender authorization or audio resolution failed; anonymous fallback is disabled")
                raise RuntimeError("Sender-authorized audio unavailable") from None
            emit("ANONYMOUS_FALLBACK", video_id=video_id,
                 reason="Explicitly enabled anonymous fallback; sender account is not used")
            info = await asyncio.to_thread(resolve, video_id)
            return {**info, "resolver": "anonymous"}

    async def start(self):
        self.resolver_warmup_task = asyncio.create_task(self.warmup_resolver())
        if self.manual:
            self.volume = {"level": 100, "muted": False}
        else:
            p = await self.ma.player(self.config["target_player_id"])
            validate_target(p, await self.ma.call("players/all", return_protocol_players=True))
            self.volume = {"level": p.get("volume_level") or 0, "muted": bool(p.get("volume_muted"))}
        self.client, source_id = await connect_source(self.ma, self.config, self.source_command)
        uri = f"sendspin_source://audio_source/{source_id}"
        self.router = ManualSession(uri) if self.manual else SessionRouter(
            self.ma, self.config["target_player_id"], uri)
        self.monitor_task = asyncio.create_task(self.monitor())
        emit("READY", state=self.state())

    async def warmup_resolver(self):
        try:
            await self.resolver.warmup()
            emit("RESOLVER_WARMED")
        except Exception:
            # Playback can retry initialization later; no anonymous substitution.
            emit("RESOLVER_WARMUP_FAILED")

    def source_command(self, payload):
        if payload.source is None:
            return
        self.source_epoch += 1
        if payload.source.command == "start":
            previous = self.capture_task
            self.capture_task = asyncio.create_task(self.start_stream(previous))
        elif payload.source.command == "stop":
            self.streaming.clear()
            if self.capture_task:
                self.capture_task.cancel()
            if self.router and self.router.active and not self.router.closing:
                asyncio.create_task(self.source_released(self.source_epoch))

    async def start_stream(self, previous):
        # An old capture's finalizer must not end a newly opened source stream.
        if previous:
            previous.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await previous
        await self.stream()

    async def stop_capture(self):
        task = self.capture_task
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if self.capture_task is task:
                self.capture_task = None
        self.streaming.clear()

    async def source_released(self, epoch=None):
        if self.manual:
            async with self.lock:
                if not self.streaming.is_set():
                    self.invalidate()
                    await self.stop_decoder()
                    self.decoder_pending = bool(self.info and self.router.active)
            return
        # Give MA a moment to publish the source transition. Never issue a
        # power-off here: the receiver could already be playing something else.
        await asyncio.sleep(0.5)
        async with self.lock:
            # Stop/start may already have replaced this capture by the time the
            # delayed callback runs. It must not cancel the resumed session.
            if epoch is not None and epoch != self.source_epoch:
                return
            if self.router.active and not self.router.closing:
                p = await self.ma.player(self.router.target)
                if owns_source(p, self.router.uri) and p.get("available") and (
                    self.router.suspended or self.streaming.is_set()
                ):
                    return
                self.router.yield_to_external()
                self.invalidate()
                await self.stop_decoder()
                emit("TAKEN_OVER")

    async def suspend_output(self):
        # Pause consumption immediately, then flush MA's queued output via Stop.
        # Keep metadata, position, source selection and power ownership intact.
        self.paused = True
        self.invalidate()
        self.decoder_pending = False
        await self.router.suspend()
        await self.stop_capture()
        await self.stop_decoder()

    async def resume_output(self):
        try:
            await self.router.resume()
            await asyncio.wait_for(self.streaming.wait(), 20)
            await self.decoder_at(self.position)
            self.paused = False
        except BaseException:
            self.paused = True
            await self.stop_decoder()
            await self.router.finish()
            raise

    async def stream(self):
        self.capture_failure = None
        capture = self.client.create_source_capture(SupportedAudioFormat(
            codec=AudioCodec.PCM, sample_rate=48000, bit_depth=16, channels=2
        ))
        try:
            await capture.start()
            self.streaming.set()
            emit("SOURCE_STREAM_STARTED")
            next_deadline = asyncio.get_running_loop().time()
            last_pcm_state = None
            while True:
                next_deadline = max(
                    next_deadline + 0.025, asyncio.get_running_loop().time()
                )
                pcm = SILENCE
                kind = "paused" if self.paused else "idle"
                read_ms = 0
                if self.manual and self.decoder_pending and not self.lock.locked():
                    async with self.lock:
                        if self.info and self.router.active and self.decoder_pending:
                            await self.decoder_at(self.position)
                            self.decoder_pending = False
                decoder = self.decoder
                generation = self.generation
                if decoder and not self.paused:
                    try:
                        read_started = asyncio.get_running_loop().time()
                        async with asyncio.timeout(15):
                            async with self.decoder_read_lock:
                                if decoder is not self.decoder:
                                    continue
                                pcm = await decoder.stdout.readexactly(FRAME_BYTES)
                        read_ms = round((asyncio.get_running_loop().time() - read_started) * 1000)
                        if generation != self.generation or decoder is not self.decoder:
                            continue
                        self.position += len(pcm) / 192000
                        kind = "audio"
                    except asyncio.IncompleteReadError as e:
                        code = await decoder.wait()
                        if decoder is self.decoder:
                            self.decoder = None
                            if code == 0:
                                emit("TRACK_ENDED", video_id=self.video_id)
                            else:
                                emit("PLAYBACK_ERROR", reason="Decoder failed")
                        pcm = e.partial + bytes(FRAME_BYTES - len(e.partial))
                feed_started = asyncio.get_running_loop().time()
                await capture.feed(apply_input_volume(pcm, self.volume) if self.manual else pcm)
                feed_ms = round((asyncio.get_running_loop().time() - feed_started) * 1000)
                pcm_state = (generation, kind)
                if pcm_state != last_pcm_state:
                    emit("PCM_TRANSITION", generation=generation, kind=kind,
                         position=round(self.position, 3), read_ms=read_ms, feed_ms=feed_ms)
                    last_pcm_state = pcm_state
                if read_ms >= 250 or feed_ms >= 250:
                    emit("PCM_STALL", generation=generation, read_ms=read_ms, feed_ms=feed_ms)
                await asyncio.sleep(max(0, next_deadline - asyncio.get_running_loop().time()))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.capture_failure = type(error).__name__
            emit("PLAYBACK_ERROR", reason="Sendspin input stream failed", error_type=type(error).__name__)
        finally:
            if asyncio.current_task() is self.capture_task:
                self.streaming.clear()
            with contextlib.suppress(Exception):
                await capture.stop()

    async def stop_decoder(self):
        proc, self.decoder = self.decoder, None
        if not proc:
            return
        started = asyncio.get_running_loop().time()
        forced_kill = False
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

        async def drain_and_wait():
            # FFmpeg may flush PCM on SIGTERM. Without a reader its full pipe
            # blocks shutdown until the kill timeout, delaying the next track.
            # Share the read lock with stream() to avoid concurrent pipe reads.
            async with self.decoder_read_lock:
                if proc.stdout:
                    while await proc.stdout.read(65536):
                        pass
            await proc.wait()

        try:
            await asyncio.wait_for(drain_and_wait(), 3)
        except TimeoutError:
            forced_kill = True
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await drain_and_wait()
        emit("DECODER_STOPPED", duration_ms=round(
            (asyncio.get_running_loop().time() - started) * 1000), forced_kill=forced_kill)

    async def decoder_at(self, position):
        started = asyncio.get_running_loop().time()
        await self.stop_decoder()
        if not self.info:
            raise RuntimeError("No track loaded")
        url = self.info.get("url")
        if not url or not url.startswith("https://"):
            raise RuntimeError("Extractor did not return an HTTPS audio stream")
        # No shell, no stderr dump: signed URLs must not enter logs.
        self.decoder = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-user_agent", (self.info.get("http_headers") or {}).get("User-Agent", "cast2sendspin"),
            "-ss", str(position), "-i", url, "-vn",
            "-ac", "2", "-ar", "48000", "-f", "s16le", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        self.position = position
        emit("DECODER_STARTED", position=position, generation=self.generation,
             duration_ms=round((asyncio.get_running_loop().time() - started) * 1000))

    async def play(self, video_id, position=0, context=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise RuntimeError("Invalid YouTube video ID")
        if not math.isfinite(position) or position < 0:
            raise RuntimeError("Invalid start position")
        async with self.lock:
            self.invalidate()
            generation = self.generation
            self.decoder_pending = False
            self.info = None
            await self.stop_decoder()
        # Resolve outside the state lock: Stop Casting can cancel ownership
        # immediately even if an upstream extractor is slow.
        task = asyncio.create_task(self.resolve_media(video_id, context))
        self.resolution_task = task
        try:
            async with asyncio.timeout(65):
                info = await task
        except asyncio.CancelledError:
            raise RuntimeError("Playback request superseded") from None
        finally:
            if self.resolution_task is task:
                self.resolution_task = None
        async with self.lock:
            if generation != self.generation:
                raise RuntimeError("Playback request superseded")
            self.info, self.video_id = info, video_id
            emit("AUDIO_RESOLVED", video_id=video_id, resolver=info.get("resolver"),
                 itag=info.get("itag"), bitrate=info.get("bitrate"))
            self.duration = float(info.get("duration") or 0)
            self.paused = True
            try:
                if not self.router.active:
                    await self.router.claim()
                if self.manual:
                    # Decode only after MA selects this input, not while the
                    # user is still choosing where to play it.
                    self.position = position
                    self.decoder_pending = True
                    self.paused = False
                    emit("TRACK_READY", video_id=video_id, title=info.get("title"))
                    return self.state()
                if self.router.suspended:
                    await self.router.resume()
                await asyncio.wait_for(self.streaming.wait(), 20)
                await self.decoder_at(position)
                self.paused = False
                # Observe a little actual PCM before telling the phone that it
                # is playing, rather than claiming success for a failing decoder.
                initial_position = self.position
                async with asyncio.timeout(20):
                    while self.position <= initial_position:
                        if self.decoder is None or self.capture_failure:
                            raise RuntimeError("Audio could not start")
                        await asyncio.sleep(0.05)
            except BaseException:
                await self.stop_decoder()
                await self.router.finish()
                raise
            emit("PLAYING", video_id=video_id, title=info.get("title"))
            return self.state()

    def state(self):
        return {"position": self.position, "duration": self.duration,
                "volume": self.volume, "paused": self.paused,
                "active": bool(self.router and self.router.active)}

    async def command(self, method, args):
        if method == "play":
            return await self.play(args["video_id"], float(args.get("position", 0)), args.get("context"))
        async with self.lock:
            if method == "state":
                return self.state()
            if method == "finish":
                self.invalidate()
                await self.stop_decoder()
                if self.router:
                    await self.router.finish()
                self.info = None
                self.decoder_pending = False
                self.position = self.duration = 0
            elif method == "track_stop":
                self.invalidate()
                await self.stop_decoder()
                self.decoder_pending = False
            elif method == "pause":
                if self.manual:
                    self.paused = True
                else:
                    await self.suspend_output()
            elif method == "resume":
                if not self.router.active:
                    raise RuntimeError("Cast no longer owns this destination")
                if not self.manual and self.router.suspended:
                    await self.resume_output()
                else:
                    self.paused = False
            elif method == "seek":
                position = float(args["position"])
                if not math.isfinite(position) or position < 0 or position > self.duration:
                    raise RuntimeError("Invalid seek position")
                if not self.router.active:
                    raise RuntimeError("Cast no longer owns this destination")
                if not self.manual:
                    was_paused = self.paused
                    await self.suspend_output()
                    self.position = position
                    if not was_paused:
                        await self.resume_output()
                elif not self.streaming.is_set():
                    self.invalidate()
                    self.position = position
                    self.decoder_pending = True
                else:
                    self.invalidate()
                    await self.decoder_at(position)
                    self.decoder_pending = False
            elif method == "volume":
                if not self.manual and not self.router.active:
                    raise RuntimeError("Cast no longer owns this destination")
                level = round(float(args["level"]))
                if not 0 <= level <= 100:
                    raise RuntimeError("Invalid volume")
                if not self.manual:
                    await self.ma.call("players/cmd/volume_set",
                                       player_id=self.router.target, volume_level=level)
                    await self.ma.call("players/cmd/volume_mute",
                                       player_id=self.router.target, muted=bool(args.get("muted")))
                self.volume = {"level": level, "muted": bool(args.get("muted"))}
            else:
                raise RuntimeError("Unknown worker method")
            return self.state()

    async def monitor(self):
        failures = 0
        while True:
            await asyncio.sleep(1)
            try:
                if self.manual:
                    emit("STATE", **self.state())
                    if not self.client.connected and self.router.active:
                        async with self.lock:
                            await self.command_manual_disconnect()
                    continue
                p = await self.ma.player(self.config["target_player_id"])
                failures = 0
                volume = {"level": p.get("volume_level") or 0, "muted": bool(p.get("volume_muted"))}
                if volume != self.volume:
                    self.volume = volume
                    emit("VOLUME_CHANGED", volume=volume)
                emit("STATE", **self.state())
                if self.router.active and not self.lock.locked():
                    if not p.get("available") or not owns_source(p, self.router.uri):
                        async with self.lock:
                            self.router.yield_to_external()
                            self.invalidate()
                            await self.stop_decoder()
                            emit("TAKEN_OVER")
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                if failures == 3:
                    # Fail closed rather than running an orphaned casting session.
                    async with self.lock:
                        self.router.yield_to_external()
                        self.invalidate()
                        await self.stop_decoder()
                    emit("PLAYBACK_ERROR", reason="MA connection lost")

    async def command_manual_disconnect(self):
        self.invalidate()
        self.decoder_pending = False
        await self.stop_decoder()
        await self.router.finish()
        emit("PLAYBACK_ERROR", reason="Sendspin source disconnected")

    async def close(self):
        if self.monitor_task:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor_task
        self.shutting_down = True
        try:
            await self.command("finish", {})
        finally:
            if self.resolver_warmup_task:
                self.resolver_warmup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.resolver_warmup_task
            await self.resolver.close()
            if self.capture_task:
                self.capture_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.capture_task
            if self.client:
                await self.client.disconnect()
            await self.ma.client.disconnect()


async def handle_command(worker, request):
    """Log command boundaries without arguments, credentials, or upstream errors."""
    started = asyncio.get_running_loop().time()
    details = {"request_id": request["id"], "method": request["method"]}
    emit("COMMAND_RECEIVED", **details)
    try:
        result = await worker.command(request["method"], request.get("args", {}))
        emit("COMMAND_APPLIED", **details, duration_ms=round(
            (asyncio.get_running_loop().time() - started) * 1000))
        emit("RESULT", id=request["id"], result=result)
    except Exception as error:
        emit("RESULT", id=request.get("id"), error=type(error).__name__)


async def main():
    config = load_config()
    async with aiohttp.ClientSession() as session:
        worker = Worker(MA(session, config["ma_url"]), config)
        tasks = set()
        reader = asyncio.StreamReader(limit=65536)
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        try:
            await worker.start()
            while line := await reader.readline():
                request = json.loads(line)
                task = asyncio.create_task(handle_command(worker, request))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await worker.close()


if __name__ == "__main__":
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        emit("FATAL", error_type=type(error).__name__)
        raise SystemExit(1)
