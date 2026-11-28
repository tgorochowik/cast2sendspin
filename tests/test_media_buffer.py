import asyncio
import contextlib
import io
import re
import shutil
import unittest
import wave
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web

from media_buffer import BufferError, CHUNK_BYTES, MAX_BYTES, MediaBuffer
from routing import ManualSession
from worker import FRAME_BYTES, Worker


class MediaBufferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = bytes(range(256)) * (CHUNK_BYTES * 8 // 256 + 4)
        self.requests = []
        self.events = []
        self.caches = []
        self.gates = {}
        self.entered = {}
        self.behavior = "normal"
        self.failures = 0
        app = web.Application()
        app.router.add_get("/media", self.origin)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{self.runner.addresses[0][1]}/media"
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        for gate in self.gates.values():
            gate.set()
        for cache in self.caches:
            await cache.close()
        await self.client.close()
        await self.runner.cleanup()

    async def origin(self, request):
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["Range"])
        start, end = map(int, match.groups())
        self.requests.append((start, end))
        if start in self.entered:
            self.entered[start].set()
        if start in self.gates:
            await self.gates[start].wait()
        if self.behavior == "ignore_range":
            return web.Response(body=self.data)
        if self.behavior == "reject":
            return web.Response(status=403)
        if self.behavior == "retry" and self.failures < 2:
            self.failures += 1
            return web.Response(status=503)
        if self.behavior == "truncate" and self.failures < 2:
            self.failures += 1
            response = web.StreamResponse(status=206, headers={
                "Content-Range": f"bytes {start}-{end}/{len(self.data)}",
                "Content-Length": str(end - start + 1),
            })
            await response.prepare(request)
            await response.write(self.data[start:start + 4096])
            request.transport.close()
            return response
        total = len(self.data) + (1 if self.behavior == "wrong_total" else 0)
        return web.Response(
            status=206, body=self.data[start:end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/{total}"},
        )

    async def cache(self, delay=3600):
        cache = MediaBuffer(
            self.url, len(self.data),
            lambda event, **fields: self.events.append((event, fields)),
            prefetch_delay=delay,
        )
        self.caches.append(cache)
        await cache.start()
        return cache

    async def read(self, cache, start, end):
        async with self.client.get(cache.url, headers={"Range": f"bytes={start}-{end}"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Length"], str(end - start + 1))
            self.assertEqual(response.headers["Content-Range"], f"bytes {start}-{end}/{len(self.data)}")
            result = await response.read()
        self.assertEqual(result, self.data[start:end + 1])

    async def test_starts_with_one_chunk_then_fetches_only_requested_ranges(self):
        cache = await self.cache()
        self.assertEqual(cache.buffered_bytes, CHUNK_BYTES)
        self.assertFalse(cache.completed.is_set())
        await self.read(cache, CHUNK_BYTES * 6 + 17, CHUNK_BYTES * 6 + 99)
        await self.read(cache, CHUNK_BYTES * 3 + 12, CHUNK_BYTES * 3 + 101)
        self.assertEqual([start for start, _ in self.requests], [0, CHUNK_BYTES * 6, CHUNK_BYTES * 3])
        await self.read(cache, CHUNK_BYTES * 6 + 17, CHUNK_BYTES * 6 + 99)
        reads = [fields for event, fields in self.events if event == "BUFFER_READ"]
        self.assertEqual([read["cache_hit"] for read in reads], [False, False, True])
        self.assertEqual(cache.buffered_bytes, CHUNK_BYTES * 3)
        self.assertNotIn(cache.url, repr(self.events))
        self.assertNotIn(self.url, repr(self.events))

    async def test_forward_and_backward_misses_do_not_wait_for_stalled_background(self):
        start = CHUNK_BYTES
        self.gates[start] = asyncio.Event()
        self.entered[start] = asyncio.Event()
        cache = await self.cache(delay=0)
        await asyncio.wait_for(self.entered[start].wait(), 1)
        await asyncio.wait_for(self.read(cache, CHUNK_BYTES * 6, CHUNK_BYTES * 6 + 1023), 1)
        await asyncio.wait_for(self.read(cache, CHUNK_BYTES * 3, CHUNK_BYTES * 3 + 1023), 1)
        self.assertFalse(self.gates[start].is_set())
        self.assertNotIn(1, cache.chunks)
        self.assertIn(6, cache.chunks)
        self.assertIn(3, cache.chunks)

    async def test_full_fetch_then_arbitrary_seeks_do_not_contact_origin(self):
        cache = await self.cache(delay=0)
        await asyncio.wait_for(cache.completed.wait(), 2)
        count = len(self.requests)
        self.behavior = "reject"
        for start in (CHUNK_BYTES * 6, 4000, CHUNK_BYTES * 3):
            await self.read(cache, start, start + 500)
        self.assertEqual(len(self.requests), count)
        self.assertEqual(cache.buffered_bytes, len(self.data))

    async def test_growing_buffer_never_reports_downloaded_length_as_eof(self):
        cache = await self.cache()
        async with self.client.get(cache.url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Length"], str(len(self.data)))
            self.assertEqual(await response.read(), self.data)
        self.assertTrue(cache.completed.is_set())

    async def test_single_flight_for_simultaneous_demand_readers(self):
        cache = await self.cache()
        start = CHUNK_BYTES * 4
        self.gates[start] = asyncio.Event()
        self.entered[start] = asyncio.Event()
        first = asyncio.create_task(cache.get_chunk(4))
        await asyncio.wait_for(self.entered[start].wait(), 1)
        second = asyncio.create_task(cache.get_chunk(4))
        await asyncio.sleep(0)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        self.gates[start].set()
        self.assertEqual(await second, self.data[start:start + CHUNK_BYTES])
        self.assertEqual(self.requests.count((start, start + CHUNK_BYTES - 1)), 1)

    async def test_cancelled_read_and_close_release_requests_server_and_memory(self):
        cache = await self.cache()
        start = CHUNK_BYTES * 4
        self.gates[start] = asyncio.Event()
        self.entered[start] = asyncio.Event()
        read = asyncio.create_task(self.client.get(cache.url, headers={"Range": f"bytes={start}-"}))
        await asyncio.wait_for(self.entered[start].wait(), 1)
        read.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await read
        url = cache.url
        await asyncio.wait_for(cache.close(), 2)
        self.assertFalse(cache.chunks)
        self.assertFalse(cache.inflight)
        self.assertIsNone(cache.source_url)
        self.assertTrue(cache.session.closed)
        with self.assertRaises(aiohttp.ClientConnectionError):
            await self.client.get(url)

    async def test_invalid_ranges_methods_and_cross_origin_access_are_rejected(self):
        cache = await self.cache()
        for value in ("bytes=-0", "bytes=99-1", "bytes=999999999-", "bytes=1-2,4-5", "nope"):
            async with self.client.get(cache.url, headers={"Range": value}) as response:
                self.assertEqual(response.status, 416)
        async with self.client.get(cache.url, headers={"Range": "bytes=-20"}) as response:
            self.assertEqual(await response.read(), self.data[-20:])
        async with self.client.head(cache.url) as response:
            self.assertEqual(response.headers["Content-Length"], str(len(self.data)))
        async with self.client.post(cache.url) as response:
            self.assertEqual(response.status, 405)
        for headers in ({"Origin": "https://example.test"}, {"Host": "attacker.test"}):
            async with self.client.get(cache.url, headers=headers) as response:
                self.assertEqual(response.status, 403)
        async with self.client.get(cache.url + "-wrong") as response:
            self.assertEqual(response.status, 404)

    async def test_bad_range_support_or_changed_representation_is_not_cached(self):
        for behavior in ("ignore_range", "wrong_total", "reject"):
            with self.subTest(behavior=behavior):
                self.behavior = behavior
                cache = MediaBuffer(self.url, len(self.data), lambda *_args, **_kwargs: None)
                self.caches.append(cache)
                with self.assertRaises(BufferError):
                    await cache.start()
                self.assertEqual(cache.buffered_bytes, 0)
                await cache.close()

    async def test_retries_transient_errors_but_never_substitutes_source_or_credentials(self):
        self.behavior = "retry"
        cache = await self.cache()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(cache.buffered_bytes, CHUNK_BYTES)

    async def test_interrupted_chunks_are_retried_without_caching_or_duplicating_partial_bytes(self):
        self.behavior = "truncate"
        cache = await self.cache()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(cache.buffered_bytes, CHUNK_BYTES)
        self.assertEqual(cache.chunks[0], self.data[:CHUNK_BYTES])

    async def test_unknown_or_excessive_sizes_are_rejected_before_network_or_allocation(self):
        for size in (None, 0, -1, True, MAX_BYTES + 1):
            with self.assertRaises(BufferError):
                MediaBuffer(self.url, size, lambda *_args, **_kwargs: None)
        self.assertFalse(self.requests)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed for the actual decoder seek test")
    async def test_real_decoder_seeks_into_full_and_incomplete_buffers_byte_accurately(self):
        pcm = bytes(range(256)) * (192000 * 30 // 256)
        encoded = io.BytesIO()
        with wave.open(encoded, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(pcm)
        self.data = encoded.getvalue()
        for full in (True, False):
            with self.subTest(full=full):
                self.behavior = "normal"
                cache = await self.cache(delay=0 if full else 3600)
                if full:
                    await asyncio.wait_for(cache.completed.wait(), 3)
                    self.behavior = "reject"
                else:
                    # Prevent the initial probe's read-ahead from warming the
                    # backward target. A seek must abandon this stalled read.
                    self.gates[8 * CHUNK_BYTES] = asyncio.Event()
                requests = len(self.requests)
                worker = Worker(Mock(), {})
                worker.info = {"url": "https://example.test/private-media"}
                worker.media_buffer = cache
                with patch("worker.emit") as emit:
                    try:
                        for position in (22.5, 15, 7.5):
                            if not full and position in (22.5, 15):
                                self.assertNotIn(int((44 + position * 192000) // CHUNK_BYTES), cache.chunks)
                            await worker.decoder_at(position)
                            actual = await asyncio.wait_for(worker.decoder.stdout.readexactly(FRAME_BYTES), 3)
                            offset = int(position * 192000)
                            self.assertEqual(actual, pcm[offset:offset + FRAME_BYTES])
                    finally:
                        await worker.stop_decoder()
                        if worker.decoder_diagnostics:
                            await asyncio.gather(*worker.decoder_diagnostics)
                self.assertFalse(any(
                    call.kwargs.get("forced_kill")
                    for call in emit.call_args_list if call.args == ("DECODER_STOPPED",)
                ))
                if full:
                    self.assertEqual(len(self.requests), requests)
                await cache.close()


class WorkerBufferTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, **config):
        worker = Worker(Mock(), {"routing_mode": "manual", **config})
        worker.router = ManualSession("sendspin_source://audio_source/test")
        worker.resolve_media = AsyncMock(return_value={
            "url": "https://example.test/private-media", "resolver": "sender",
            "duration": 216, "content_length": 7099867,
        })
        return worker

    async def test_current_track_is_always_buffered_even_with_obsolete_opt_out(self):
        for value in (False, "false", None):
            worker = self.worker(media_buffer_enabled=value)
            cache = Mock(start=AsyncMock(), close=AsyncMock())
            with patch("worker.MediaBuffer", return_value=cache) as constructor:
                _, prepared = await worker.prepare_track("DPXHMBKY39M", {})
            self.assertIs(prepared, cache)
            constructor.assert_called_once()
            cache.start.assert_awaited_once()

    async def test_failed_initialization_closes_buffer_and_keeps_direct_streaming(self):
        worker = self.worker(media_buffer_enabled=True)
        cache = Mock(start=AsyncMock(side_effect=BufferError("range_unsupported")), close=AsyncMock())
        with patch("worker.MediaBuffer", return_value=cache), patch("worker.emit") as emit:
            info, prepared = await worker.prepare_track("DPXHMBKY39M", {})
        self.assertIsNone(prepared)
        self.assertEqual(info["url"], "https://example.test/private-media")
        cache.close.assert_awaited_once()
        self.assertNotIn("private-media", repr(emit.call_args_list))

    async def test_stop_cancels_buffer_preparation_before_claiming_destination(self):
        worker = self.worker(media_buffer_enabled=True)
        entered = asyncio.Event()

        async def start():
            entered.set()
            await asyncio.Event().wait()

        cache = Mock(start=AsyncMock(side_effect=start), close=AsyncMock())
        with patch("worker.MediaBuffer", return_value=cache):
            task = asyncio.create_task(worker.play("DPXHMBKY39M", context={"ctt": "synthetic"}))
            await asyncio.wait_for(entered.wait(), 1)
            await worker.command("finish", {})
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                await asyncio.wait_for(task, 1)
        cache.close.assert_awaited_once()
        self.assertIsNone(worker.media_buffer)
        self.assertFalse(worker.router.active)

    async def test_finish_and_track_replacement_release_current_track_buffer(self):
        for method in ("finish", "track_stop"):
            worker = self.worker()
            cache = Mock(close=AsyncMock())
            worker.media_buffer = cache
            await worker.command(method, {})
            cache.close.assert_awaited_once()
            self.assertIsNone(worker.media_buffer)
