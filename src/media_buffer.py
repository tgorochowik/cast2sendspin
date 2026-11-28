"""Session-local encoded audio cache with seek-prioritized HTTP range fetching.

Nothing is written to disk. The source URL is private; the loopback URL is a
per-track capability and must never enter logs. No YouTube API calls live here.
"""
import asyncio
import re
import secrets

import aiohttp
from aiohttp import web


CHUNK_BYTES = 256 * 1024
MAX_BYTES = 32 * 1024 * 1024


class BufferError(RuntimeError):
    """Only fixed, local reason codes; never an upstream error or URL."""


class MediaBuffer:
    def __init__(self, source_url, size, on_event, *, user_agent="cast2sendspin",
                 prefetch_delay=0):
        if type(size) is not int or not 0 < size <= MAX_BYTES:
            raise BufferError("unsupported_size")
        self.source_url = source_url
        self.size = size
        self.on_event = on_event
        self.user_agent = user_agent
        self.prefetch_delay = prefetch_delay
        self.chunks = {}
        self.buffered_bytes = 0
        self.closed = False
        self.url = None
        self.session = None
        self.runner = None
        self.prefetch = None
        self.inflight = {}
        self.waiters = {}
        self.handlers = {}
        # Background fill uses at most one slot, leaving one for a cache miss.
        self.slots = asyncio.Semaphore(2)
        self.started = None
        self.completed = asyncio.Event()

    def snapshot(self):
        return {
            "buffered_bytes": self.buffered_bytes, "total_bytes": self.size,
            "buffer_complete": self.completed.is_set(),
            "prefetch_delay_ms": round(self.prefetch_delay * 1000),
        }

    async def start(self):
        self.started = asyncio.get_running_loop().time()
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=2),
            cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=3, connect=2, sock_read=2),
        )
        # Validate range support and representation length before using a local
        # URL. Only one small chunk is required, not the complete track.
        await self.get_chunk(0)
        app = web.Application()
        app.router.add_route("*", "/" + secrets.token_urlsafe(24), self.serve)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True,
                                    shutdown_timeout=1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        path = next(iter(app.router.resources())).canonical
        self.url = f"http://127.0.0.1:{port}{path}"
        self.on_event("BUFFER_READY", **self.snapshot())
        self.prefetch = asyncio.create_task(self.fill())
        return self

    async def get_chunk(self, index):
        if self.closed:
            raise BufferError("closed")
        if index in self.chunks:
            return self.chunks[index]
        task = self.inflight.get(index)
        if task is None:
            task = asyncio.create_task(self.fetch_chunk(index))
            self.inflight[index] = task
            self.waiters[index] = 0
        self.waiters[index] += 1
        try:
            # Share a background fetch if a reader needs those same bytes.
            return await asyncio.shield(task)
        finally:
            self.waiters[index] -= 1
            if self.waiters[index] == 0:
                self.waiters.pop(index)
                self.inflight.pop(index)
                if not task.done():
                    task.cancel()
                # Observe exceptions and release HTTP slots even on disconnect.
                await asyncio.gather(task, return_exceptions=True)

    async def fetch_chunk(self, index):
        start = index * CHUNK_BYTES
        end = min(start + CHUNK_BYTES, self.size) - 1
        if start < 0 or start > end:
            raise BufferError("invalid_range")
        async with self.slots:
            for attempt in range(3):
                try:
                    async with self.session.get(
                        self.source_url, allow_redirects=False,
                        headers={"Range": f"bytes={start}-{end}",
                                 "Accept-Encoding": "identity", "User-Agent": self.user_agent},
                    ) as response:
                        # Never mix partial/unknown representations or silently
                        # read the whole file when a server ignores Range.
                        expected = f"bytes {start}-{end}/{self.size}"
                        if response.status >= 500:
                            raise aiohttp.ClientConnectionError()
                        if (response.status != 206
                                or response.headers.get("Content-Range") != expected
                                or response.headers.get("Content-Encoding", "identity") != "identity"):
                            raise BufferError("range_unsupported")
                        payload = bytearray()
                        async for part in response.content.iter_chunked(65536):
                            payload.extend(part)
                            if len(payload) > end - start + 1:
                                raise BufferError("invalid_length")
                        if len(payload) != end - start + 1:
                            raise aiohttp.ClientPayloadError()
                        data = bytes(payload)
                        self.chunks[index] = data
                        self.buffered_bytes += len(data)
                        self.on_event("BUFFER_PROGRESS", **self.snapshot())
                        if self.buffered_bytes == self.size:
                            self.completed.set()
                            self.on_event("BUFFER_COMPLETE", **self.snapshot(), duration_ms=round(
                                (asyncio.get_running_loop().time() - self.started) * 1000))
                        return data
                except (aiohttp.ClientError, TimeoutError):
                    self.on_event("BUFFER_RETRY", byte_offset=start, attempt=attempt + 1)
            raise BufferError("fetch_failed")

    async def fill(self):
        try:
            for index in range((self.size + CHUNK_BYTES - 1) // CHUNK_BYTES):
                if self.prefetch_delay:
                    await asyncio.sleep(self.prefetch_delay)
                await self.get_chunk(index)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Demand reads may retry later; valid cached ranges remain useful.
            self.on_event("BUFFER_BACKGROUND_FAILED", error_type=type(error).__name__)

    def bounds(self, value):
        if value is None:
            return 0, self.size - 1, 200
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
        if not match or not any(match.groups()):
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{self.size}"})
        first, last = match.groups()
        if first:
            start, end = int(first), min(int(last), self.size - 1) if last else self.size - 1
        else:
            start, end = max(0, self.size - int(last)), self.size - 1
        if start > end or start >= self.size:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{self.size}"})
        return start, end, 206

    async def serve(self, request):
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])
        if request.headers.get("Origin") or request.host != self.url.split("/")[2]:
            raise web.HTTPForbidden()
        start, end, status = self.bounds(request.headers.get("Range"))
        headers = {
            "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
            "Content-Type": "application/octet-stream", "Cache-Control": "no-store",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{self.size}"
        if request.method == "HEAD":
            return web.Response(status=status, headers=headers)
        task = asyncio.current_task()
        self.handlers[task] = request.transport
        response = web.StreamResponse(status=status, headers=headers)
        try:
            index = start // CHUNK_BYTES
            hit = index in self.chunks
            started = asyncio.get_running_loop().time()
            data = await self.get_chunk(index)
            self.on_event("BUFFER_READ", byte_offset=start, cache_hit=hit,
                          duration_ms=round((asyncio.get_running_loop().time() - started) * 1000),
                          **self.snapshot())
            await response.prepare(request)
            offset = start
            while offset <= end:
                stop = min((index + 1) * CHUNK_BYTES, end + 1)
                await response.write(data[offset % CHUNK_BYTES:stop - index * CHUNK_BYTES])
                offset, index = stop, index + 1
                if offset <= end:
                    data = await self.get_chunk(index)
            await response.write_eof()
            return response
        except (ConnectionResetError, BrokenPipeError):
            # Decoder replacement closes an old local reader intentionally.
            return response
        except (BufferError, aiohttp.ClientError, TimeoutError) as error:
            self.on_event("BUFFER_READ_FAILED", error_type=type(error).__name__)
            if response.prepared:
                # Headers promise the real length. Never manufacture a normal
                # EOF at the current downloaded length of a growing buffer.
                if request.transport:
                    request.transport.close()
                return response
            raise web.HTTPBadGateway(text="Media range unavailable") from None
        finally:
            self.handlers.pop(task, None)

    async def disconnect_readers(self):
        """Interrupt old decoder reads without discarding cached track bytes."""
        handlers = list(self.handlers.items())
        for task, transport in handlers:
            if transport:
                transport.close()
            task.cancel()
        await asyncio.gather(*(task for task, _ in handlers), return_exceptions=True)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.disconnect_readers()
        tasks = [*self.inflight.values(), *self.handlers]
        if self.prefetch:
            tasks.append(self.prefetch)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        if self.session:
            await self.session.close()
        self.on_event("BUFFER_CLOSED", **self.snapshot())
        self.chunks.clear()
        self.source_url = None
        self.url = None
