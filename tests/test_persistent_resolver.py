import asyncio
import json
import shutil
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sender_resolver import SenderResolver


class PersistentResolverTests(unittest.IsolatedAsyncioTestCase):
    def process(self):
        proc = Mock(returncode=None)
        proc.stdin = Mock(drain=AsyncMock())
        proc.communicate = AsyncMock(return_value=(b"", None))

        async def response():
            request = json.loads(proc.stdin.write.call_args.args[0])
            reply = {"request_id": request["request_id"]}
            if request["op"] == "warmup":
                reply["ready"] = True
            else:
                reply["info"] = {"url": "https://r1.googlevideo.com/audio", "resolver": "sender"}
            return json.dumps(reply).encode() + b"\n"

        proc.stdout = Mock(readline=AsyncMock(side_effect=response))
        return proc

    async def test_warmup_and_resolution_reuse_one_process_with_private_stdin(self):
        proc = self.process()
        resolver = SenderResolver()
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
            try:
                await resolver.warmup()
                await resolver.resolve("ZFZM6jDTWd4", {"ctt": "synthetic-secret", "unrelated": "omit"})
                await resolver.resolve("nwWVxPaOdN4", {"ctt": "different-sender"})
                spawn.assert_awaited_once()
                self.assertEqual(spawn.call_args.args, ("node", "src/youtube-resolver.mjs", "--serve"))
                requests = [json.loads(c.args[0]) for c in proc.stdin.write.call_args_list]
                self.assertEqual([r["request_id"] for r in requests], [1, 2, 3])
                self.assertEqual(requests[1]["context"], {"ctt": "synthetic-secret"})
                self.assertEqual(requests[2]["context"], {"ctt": "different-sender"})
            finally:
                await resolver.close()
        proc.kill.assert_called_once()
        proc.communicate.assert_awaited_once()

    async def test_cancellation_kills_and_reaps_process_before_next_request(self):
        old, new = self.process(), self.process()
        resolver = SenderResolver()
        started = asyncio.Event()

        async def stalled():
            started.set()
            await asyncio.Event().wait()

        old.stdout.readline.side_effect = stalled
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock(side_effect=[old, new])):
            task = asyncio.create_task(resolver.resolve("ZFZM6jDTWd4", {"ctt": "old-secret"}))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            old.kill.assert_called_once()
            old.communicate.assert_awaited_once()
            try:
                self.assertEqual((await resolver.resolve("nwWVxPaOdN4", {"ctt": "new"}))["resolver"], "sender")
            finally:
                await resolver.close()

    async def test_bad_response_resets_process_and_next_call_can_recover(self):
        for response in [b"", b"not-json\n", b'{"request_id":999}\n',
                         b'{"request_id":1,"info":null}\n', b"x" * 65537 + b"\n"]:
            with self.subTest(response=response[:40]):
                old, new = self.process(), self.process()
                old.stdout.readline.side_effect = None
                old.stdout.readline.return_value = response
                resolver = SenderResolver()
                with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock(side_effect=[old, new])):
                    with self.assertRaises((RuntimeError, ValueError)):
                        await resolver.resolve("ZFZM6jDTWd4", {"ctt": "synthetic"})
                    old.kill.assert_called_once()
                    try:
                        await resolver.resolve("nwWVxPaOdN4", {"ctt": "different"})
                    finally:
                        await resolver.close()

    async def test_upstream_error_is_sanitized_without_an_automatic_replay(self):
        proc = self.process()
        proc.stdout.readline.side_effect = None
        proc.stdout.readline.return_value = b'{"request_id":1,"error":"secret signed-url"}\n'
        resolver = SenderResolver()
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            try:
                with self.assertRaisesRegex(RuntimeError, "^Sender-authorized resolution failed$"):
                    await resolver.resolve("ZFZM6jDTWd4", {"ctt": "synthetic"})
                proc.stdin.write.assert_called_once()
            finally:
                await resolver.close()

    async def test_timeout_resets_process(self):
        proc = self.process()
        resolver = SenderResolver()

        async def stalled():
            await asyncio.Event().wait()

        proc.stdout.readline.side_effect = stalled
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with self.assertRaises(TimeoutError):
                await resolver._request({"op": "warmup"}, timeout=0.01)
        proc.kill.assert_called_once()
        proc.communicate.assert_awaited_once()
        self.assertIsNone(resolver.process)

    async def test_exited_process_is_reaped_and_replaced(self):
        old, new = self.process(), self.process()
        resolver = SenderResolver()
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock(side_effect=[old, new])):
            try:
                await resolver.warmup()
                old.returncode = 1
                await resolver.resolve("ZFZM6jDTWd4", {"ctt": "synthetic"})
                old.communicate.assert_awaited_once()
                old.kill.assert_not_called()
                self.assertIs(resolver.process, new)
            finally:
                await resolver.close()

    async def test_simultaneous_requests_do_not_interleave_pipe_messages(self):
        proc = self.process()
        resolver = SenderResolver()
        started, release = asyncio.Event(), asyncio.Event()
        original_read = proc.stdout.readline.side_effect

        async def read():
            if proc.stdin.write.call_count == 1:
                started.set()
                await release.wait()
            return await original_read()

        proc.stdout.readline.side_effect = read
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            first = asyncio.create_task(resolver.resolve("ZFZM6jDTWd4", {"ctt": "one"}))
            await asyncio.wait_for(started.wait(), 1)
            second = asyncio.create_task(resolver.resolve("nwWVxPaOdN4", {"ctt": "two"}))
            try:
                proc.stdin.write.assert_called_once()
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 1)
                self.assertEqual(proc.stdin.write.call_count, 2)
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
                await resolver.close()

    async def test_oversized_credentials_are_rejected_before_spawning(self):
        resolver = SenderResolver()
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaisesRegex(RuntimeError, "too large"):
                await resolver.resolve("ZFZM6jDTWd4", {"ctt": "x" * 50000})
            spawn.assert_not_awaited()
        await resolver.close()

    async def test_only_safe_numeric_timing_fields_are_forwarded(self):
        proc = self.process()
        proc.stdout.readline.side_effect = None
        proc.stdout.readline.return_value = json.dumps({
            "request_id": 1, "info": {"resolver": "sender"},
            "timings": {"init_ms": 5, "player_ms": "secret", "access_ms": -1,
                        "total_ms": 20, "cache_hit": True, "url": "secret"},
        }).encode() + b"\n"
        report = Mock()
        resolver = SenderResolver(report)
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            try:
                await resolver.resolve("ZFZM6jDTWd4", {"ctt": "synthetic"})
                report.assert_called_once_with(init_ms=5, total_ms=20, cache_hit=True)
            finally:
                await resolver.close()

    async def test_closed_resolver_cannot_spawn_again(self):
        resolver = SenderResolver()
        await resolver.close()
        with patch("sender_resolver.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaisesRegex(RuntimeError, "closed"):
                await resolver.warmup()
            spawn.assert_not_awaited()

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    async def test_real_node_protocol_reuses_pid_without_network(self):
        # Exercise the shipped protocol with synthetic media, never YouTube or MA.
        script = """
import { serve } from './src/youtube-resolver.mjs';
await serve(process.stdin, process.stdout, {
  async warmup() {},
  async resolve() { return { info: { resolver: 'sender', url: 'https://example.test/audio' } }; }
});
"""
        real_spawn = asyncio.create_subprocess_exec

        async def spawn(*_, **kwargs):
            return await real_spawn("node", "--input-type=module", "-e", script, **kwargs)

        resolver = SenderResolver()
        with patch("sender_resolver.asyncio.create_subprocess_exec", side_effect=spawn):
            try:
                await resolver.warmup()
                pid = resolver.process.pid
                await resolver.resolve("ZFZM6jDTWd4", {"ctt": "synthetic"})
                await resolver.resolve("nwWVxPaOdN4", {"ctt": "another"})
                self.assertEqual(resolver.process.pid, pid)
            finally:
                await resolver.close()
