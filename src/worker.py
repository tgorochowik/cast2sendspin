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
from app_config import load_config, stop_receiver_on_seek
from routing import ManualSession, SessionRouter, owns_source, validate_target
from source_client import connect_source
from sender_resolver import SenderResolver
from media_buffer import MediaBuffer

FRAME_BYTES = 4800  # 25 ms, 48 kHz, stereo s16le
SILENCE = bytes(FRAME_BYTES)
# Resume interrupted seekable HTTP input at its encoded-byte offset, before
# demuxer EOF. Do not reconnect_at_eof or reconnect_streamed: a real EOF must
# finish, and a non-seekable stream must not be replayed from the beginning.
FFMPEG_HTTP_OPTIONS = (
    "-reconnect", "1",
    "-reconnect_on_network_error", "1",
    "-reconnect_delay_max", "2",
    "-reconnect_max_retries", "3",
    "-reconnect_delay_total_max", "5",
    "-rw_timeout", "10000000",
)


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
        self.continuous_seek = not stop_receiver_on_seek(config) and not self.manual
        self.nonblocking_pcm = self.continuous_seek
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
        self.decoder_diagnostics = set()
        self.media_buffer = None
        self.seek_timing = None
        self.pcm_read_task = None

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

    async def prepare_track(self, video_id, context):
        info = await self.resolve_media(video_id, context)
        cache = None
        try:
            if info.get("resolver") != "sender":
                raise ValueError("Only sender-authorized finite media is buffered")
            delay = self.config.get("media_buffer_prefetch_delay_ms", 0)
            if type(delay) is not int or not 0 <= delay <= 5000:
                raise ValueError("Invalid buffer test delay")
            cache = MediaBuffer(
                info["url"], info.get("content_length"), emit,
                user_agent=(info.get("http_headers") or {}).get("User-Agent", "cast2sendspin"),
                prefetch_delay=delay / 1000,
            )
            async with asyncio.timeout(3):
                await cache.start()
        except BaseException as error:
            if cache:
                await cache.close()
            cache = None
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            # Unknown size, ignored Range, expired URL, excessive size or
            # startup timeout: retain the existing direct-streaming path.
            emit("BUFFER_UNAVAILABLE", error_type=type(error).__name__)
        return info, cache

    async def stop_buffer(self):
        cache, self.media_buffer = self.media_buffer, None
        if cache:
            await cache.close()

    def buffer_snapshot(self):
        return {
            "seek_output_mode": "continuous" if self.continuous_seek else "interrupt",
            **({"mode": "buffered", **self.media_buffer.snapshot()}
               if self.media_buffer else {"mode": "direct"}),
        }

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
                await self.stop_buffer()
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

    async def seek_continuously(self, position):
        """Replace decoder input, retaining the already-owned output stream."""
        await self.router.require_ownership()
        self.invalidate()
        self.decoder_pending = False
        try:
            await self.decoder_at(position)
            # Never keep feeding a destination taken over during replacement.
            await self.router.require_ownership()
            if not self.streaming.is_set():
                raise RuntimeError("Source stream ended during seek")
        except BaseException:
            self.seek_timing = None
            self.paused = True
            await self.stop_decoder()
            await self.stop_buffer()
            await self.router.finish()
            raise

    async def read_pcm(self, decoder):
        started = asyncio.get_running_loop().time()
        async with asyncio.timeout(15):
            async with self.decoder_read_lock:
                pcm = await decoder.stdout.readexactly(FRAME_BYTES)
        return pcm, round((asyncio.get_running_loop().time() - started) * 1000)

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
                        ready = True
                        if self.nonblocking_pcm:
                            if self.pcm_read_task is None:
                                self.pcm_read_task = asyncio.create_task(self.read_pcm(decoder))
                            if self.pcm_read_task.done():
                                task, self.pcm_read_task = self.pcm_read_task, None
                                pcm, read_ms = task.result()
                            else:
                                # Do not stall capture while waiting for a new
                                # decoder/network read; maintain its 25 ms clock.
                                ready, kind = False, "buffering"
                        else:
                            pcm, read_ms = await self.read_pcm(decoder)
                        if ready:
                            if generation != self.generation or decoder is not self.decoder:
                                continue
                            self.position += len(pcm) / 192000
                            kind = "audio"
                            if self.nonblocking_pcm:
                                # Read the next frame between capture ticks,
                                # rather than accidentally running at half rate.
                                self.pcm_read_task = asyncio.create_task(self.read_pcm(decoder))
                    except asyncio.IncompleteReadError as e:
                        code = await decoder.wait()
                        emit("DECODER_EOF", video_id=self.video_id,
                             position=round(self.position, 3), duration=self.duration,
                             returncode=code, partial_bytes=len(e.partial),
                              generation=generation, current_generation=self.generation,
                              current_decoder=decoder is self.decoder)
                        if generation != self.generation or decoder is not self.decoder:
                            continue
                        if decoder is self.decoder:
                            self.decoder = None
                            expected = (self.info or {}).get("audio_duration") or self.duration
                            if math.isfinite(expected) and expected > self.position + 2:
                                emit("PLAYBACK_ERROR", reason="Audio stream ended before track completion",
                                     video_id=self.video_id, position=round(self.position, 3),
                                     duration=expected)
                            elif code == 0:
                                emit("TRACK_ENDED", video_id=self.video_id)
                            else:
                                emit("PLAYBACK_ERROR", reason="Decoder failed")
                        pcm = e.partial + bytes(FRAME_BYTES - len(e.partial))
                feed_started = asyncio.get_running_loop().time()
                await capture.feed(apply_input_volume(pcm, self.volume) if self.manual else pcm)
                feed_ms = round((asyncio.get_running_loop().time() - feed_started) * 1000)
                timing = self.seek_timing
                if kind == "audio" and timing and timing.get("generation") == generation:
                    emit("SEEK_AUDIO", generation=generation,
                         requested_position=timing["position"], position=round(self.position, 3),
                         duration_ms=round((asyncio.get_running_loop().time() - timing["started"]) * 1000),
                         **{**self.buffer_snapshot(),
                            "seek_output_mode": timing.get("output_mode", "interrupt")})
                    self.seek_timing = None
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
        started = asyncio.get_running_loop().time()
        proc, self.decoder = self.decoder, None
        task, self.pcm_read_task = self.pcm_read_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not proc:
            return
        forced_kill = False
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        if self.media_buffer:
            # Wake a decoder blocked on an uncached local HTTP range. Keep the
            # cached bytes/background fill, but do not wait for network timeout
            # before it can observe SIGTERM during a rapid seek.
            await self.media_buffer.disconnect_readers()

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
        if self.media_buffer:
            url = self.media_buffer.url
        input_options = (("-rw_timeout", "10000000") if self.media_buffer
                         else FFMPEG_HTTP_OPTIONS)
        # No shell or raw stderr dump: signed URLs must not enter logs.
        self.decoder = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "verbose",
            *input_options,
            "-user_agent", (self.info.get("http_headers") or {}).get("User-Agent", "cast2sendspin"),
            "-ss", str(position), "-i", url, "-vn",
            "-ac", "2", "-ar", "48000", "-f", "s16le", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        diagnostic = asyncio.create_task(self.read_decoder_diagnostics(self.decoder, self.generation))
        self.decoder_diagnostics.add(diagnostic)
        diagnostic.add_done_callback(self.decoder_diagnostics.discard)
        self.position = position
        if self.nonblocking_pcm:
            self.pcm_read_task = asyncio.create_task(self.read_pcm(self.decoder))
        if self.seek_timing:
            self.seek_timing["generation"] = self.generation
        emit("DECODER_STARTED", position=position, generation=self.generation,
             duration_ms=round((asyncio.get_running_loop().time() - started) * 1000))

    async def read_decoder_diagnostics(self, decoder, generation):
        """Drain stderr using bounded memory; expose only fixed reason codes."""
        patterns = {
            b"stream ends prematurely": "http_truncated",
            b"file ended prematurely": "container_truncated",
            b"connection reset": "connection_reset",
            b"http error": "http_error",
            b"input/output error": "io_error",
            b"invalid data": "invalid_data",
            b"packet corrupt": "corrupt_packet",
            b"connection timed out": "connection_timeout",
            b"will reconnect at": "http_reconnect",
        }
        seen, suffix, stats = set(), b"", {}
        while chunk := await decoder.stderr.read(4096):
            data = suffix + chunk.lower()
            for pattern, reason in patterns.items():
                if pattern in data and reason not in seen:
                    seen.add(reason)
                    emit("DECODER_DIAGNOSTIC", reason=reason, generation=generation)
            match = re.search(rb"statistics: (\d+) bytes read, (\d+) seeks\s*\n", data)
            if match:
                stats.update(bytes_read=int(match[1]), input_seeks=int(match[2]))
            match = re.search(rb"duration: (\d+):(\d+):(\d+\.\d+),", data)
            if match:
                stats["container_duration"] = (
                    int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
                )
            suffix = data[-256:]
        emit("DECODER_INPUT_STATS", generation=generation, **stats)

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
            self.seek_timing = None
            await self.stop_decoder()
            await self.stop_buffer()
        # Resolve outside the state lock: Stop Casting can cancel ownership
        # immediately even if an upstream extractor is slow.
        task = asyncio.create_task(self.prepare_track(video_id, context))
        self.resolution_task = task
        try:
            async with asyncio.timeout(65):
                info, cache = await task
        except asyncio.CancelledError:
            # Cancellation can race a completed preparation whose result has
            # not yet been adopted by this playback generation.
            if task.done() and not task.cancelled():
                with contextlib.suppress(Exception):
                    _, unused = task.result()
                    if unused:
                        await unused.close()
            raise RuntimeError("Playback request superseded") from None
        finally:
            if self.resolution_task is task:
                self.resolution_task = None
        try:
            await self.lock.acquire()
        except BaseException:
            if cache:
                await cache.close()
            raise
        try:
            if generation != self.generation:
                if cache:
                    await cache.close()
                raise RuntimeError("Playback request superseded")
            self.info, self.video_id, self.media_buffer = info, video_id, cache
            emit("AUDIO_RESOLVED", video_id=video_id, resolver=info.get("resolver"),
                 itag=info.get("itag"), bitrate=info.get("bitrate"),
                 duration=info.get("duration"), audio_duration=info.get("audio_duration"),
                 content_length=info.get("content_length"))
            self.duration = float(info.get("duration") or 0)
            emit("MEDIA_MODE", video_id=video_id, **self.buffer_snapshot())
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
                await self.stop_buffer()
                await self.router.finish()
                raise
            emit("PLAYING", video_id=video_id, title=info.get("title"))
            return self.state()
        finally:
            self.lock.release()

    def state(self):
        return {"position": self.position, "duration": self.duration,
                "volume": self.volume, "paused": self.paused,
                "active": bool(self.router and self.router.active)}

    async def command(self, method, args):
        if method == "configure_seek":
            value = args.get("stop_receiver_on_seek")
            if type(value) is not bool:
                raise ValueError("Seek stop setting must be boolean")
            self.config["stop_receiver_on_seek"] = value
            self.continuous_seek = not value and not self.manual
            # Once enabled, retain the nonblocking read pump until shutdown.
            # A policy toggle must not cancel a live frame read, restart the
            # capture, or change an in-flight seek's already-selected strategy.
            self.nonblocking_pcm = self.nonblocking_pcm or self.continuous_seek
            emit("PLAYBACK_SETTINGS_APPLIED", stop_receiver_on_seek=value)
            return self.state()
        if method == "play":
            return await self.play(args["video_id"], float(args.get("position", 0)), args.get("context"))
        async with self.lock:
            if method == "state":
                return self.state()
            if method == "finish":
                self.invalidate()
                self.seek_timing = None
                await self.stop_decoder()
                await self.stop_buffer()
                if self.router:
                    await self.router.finish()
                self.info = None
                self.decoder_pending = False
                self.position = self.duration = 0
            elif method == "track_stop":
                self.invalidate()
                self.seek_timing = None
                await self.stop_decoder()
                await self.stop_buffer()
                self.decoder_pending = False
            elif method == "pause":
                self.seek_timing = None
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
                continuous = (self.continuous_seek and not self.paused
                              and not self.router.suspended and self.streaming.is_set())
                emit("SEEK_STARTED", requested_position=position, position=round(self.position, 3),
                     **{**self.buffer_snapshot(),
                        "seek_output_mode": "continuous" if continuous else "interrupt"})
                self.seek_timing = (
                    {"started": asyncio.get_running_loop().time(), "position": position,
                     "output_mode": "continuous" if continuous else "interrupt"}
                    if not self.paused and self.streaming.is_set() else None
                )
                if continuous:
                    await self.seek_continuously(position)
                elif not self.manual:
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
                            await self.stop_buffer()
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
                        await self.stop_buffer()
                    emit("PLAYBACK_ERROR", reason="MA connection lost")

    async def command_manual_disconnect(self):
        self.invalidate()
        self.decoder_pending = False
        await self.stop_decoder()
        await self.stop_buffer()
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
            await self.stop_buffer()
            if self.resolver_warmup_task:
                self.resolver_warmup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.resolver_warmup_task
            await self.resolver.close()
            if self.capture_task:
                self.capture_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.capture_task
            if self.decoder_diagnostics:
                await asyncio.gather(*self.decoder_diagnostics)
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
