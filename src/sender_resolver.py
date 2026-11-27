"""Bounded private RPC to a per-receiver, persistent YouTube resolver."""
import asyncio
import contextlib
import json


class SenderResolver:
    def __init__(self, on_timing=None):
        self.process = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.closed = False
        self.on_timing = on_timing

    async def _stop(self):
        proc, self.process = self.process, None
        if proc:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            # Drain any partial response so a full stdout pipe cannot block reap.
            await proc.communicate()

    async def _request(self, message, timeout=60):
        async with self.lock:
            if self.closed:
                raise RuntimeError("Sender resolver is closed")
            self.sequence += 1
            request_id = self.sequence
            payload = json.dumps({**message, "request_id": request_id}).encode()
            if len(payload) > 49152:
                raise RuntimeError("Sender authorization context too large")
            try:
                async with asyncio.timeout(timeout):
                    if self.process is not None and self.process.returncode is not None:
                        await self._stop()
                    if self.process is None:
                        self.process = await asyncio.create_subprocess_exec(
                            "node", "src/youtube-resolver.mjs", "--serve",
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL, limit=65536,
                        )
                    self.process.stdin.write(payload + b"\n")
                    await self.process.stdin.drain()
                    output = await self.process.stdout.readline()
                    if not output or len(output) > 65536 or not output.endswith(b"\n"):
                        raise RuntimeError("Invalid sender resolver response")
                    response = json.loads(output)
                    if not isinstance(response, dict) or response.get("request_id") != request_id:
                        raise RuntimeError("Invalid sender resolver response")
                    if not response.get("error"):
                        if message["op"] == "warmup" and response.get("ready") is not True:
                            raise RuntimeError("Invalid sender resolver response")
                        if message["op"] == "resolve":
                            info = response.get("info")
                            if not isinstance(info, dict) or info.get("resolver") != "sender":
                                raise RuntimeError("Invalid sender resolver result")
            except BaseException:
                # Cancellation must not let the old result satisfy the next
                # request, or leave its sender token attached to a warm session.
                await self._stop()
                raise
            if response.get("error"):
                raise RuntimeError("Sender-authorized resolution failed")
            return response

    async def warmup(self):
        await self._request({"op": "warmup"}, timeout=30)

    async def resolve(self, video_id, context):
        context = context if isinstance(context, dict) else {}
        response = await self._request({
            "op": "resolve", "id": video_id,
            "context": {key: context[key] for key in ("ctt", "params") if key in context},
        })
        if self.on_timing:
            timing = response.get("timings", {})
            safe = {
                key: value for key, value in timing.items()
                if key in ("init_ms", "player_ms", "decipher_ms", "access_ms", "total_ms")
                and type(value) is int and 0 <= value <= 120000
            } if isinstance(timing, dict) else {}
            if isinstance(timing, dict) and type(timing.get("cache_hit")) is bool:
                safe["cache_hit"] = timing["cache_hit"]
            self.on_timing(**safe)
        return response["info"]

    async def close(self):
        async with self.lock:
            self.closed = True
            await self._stop()
