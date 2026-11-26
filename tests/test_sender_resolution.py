import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from routing import ManualSession
from worker import Worker, resolve_sender


class SenderResolutionTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, **config):
        worker = Worker(Mock(), {"routing_mode": "manual", **config})
        worker.router = ManualSession("sendspin_source://audio_source/test")
        return worker

    async def test_sender_is_default_and_context_reaches_resolver(self):
        worker = self.worker()
        info = {"url": "https://r1.googlevideo.com/audio", "resolver": "sender"}
        with patch("worker.resolve_sender", AsyncMock(return_value=info)) as sender:
            with patch("worker.resolve") as anonymous:
                result = await worker.resolve_media("ZFZM6jDTWd4", {"ctt": "synthetic"})
        sender.assert_awaited_once_with("ZFZM6jDTWd4", {"ctt": "synthetic"})
        anonymous.assert_not_called()
        self.assertEqual(result["resolver"], "sender")

    async def test_failure_never_falls_back_by_default(self):
        for config in ({}, {"allow_anonymous_fallback": False}, {"allow_anonymous_fallback": "true"}):
            worker = self.worker(**config)
            with patch("worker.resolve_sender", AsyncMock(side_effect=RuntimeError("SECRET"))):
                with patch("worker.resolve") as anonymous:
                    with self.assertRaisesRegex(RuntimeError, "Sender-authorized audio unavailable"):
                        await worker.resolve_media("ZFZM6jDTWd4", {})
            anonymous.assert_not_called()

    async def test_explicit_fallback_is_labelled_and_logged(self):
        worker = self.worker(allow_anonymous_fallback=True)
        with patch("worker.resolve_sender", AsyncMock(side_effect=RuntimeError)):
            with patch("worker.resolve", return_value={"url": "https://example.test/audio"}):
                with patch("worker.emit") as emit:
                    result = await worker.resolve_media("ZFZM6jDTWd4", {})
        self.assertEqual(result["resolver"], "anonymous")
        self.assertEqual(emit.call_args.args[0], "ANONYMOUS_FALLBACK")

    async def test_stop_cancels_resolution_before_claiming_source(self):
        worker = self.worker()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow(*_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch("worker.resolve_sender", slow):
            task = asyncio.create_task(worker.play("ZFZM6jDTWd4", context={"ctt": "synthetic"}))
            await asyncio.wait_for(started.wait(), 1)
            await worker.command("finish", {})
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(worker.router.active)
        self.assertIsNone(worker.info)

    async def test_subprocess_receives_credentials_only_on_stdin(self):
        proc = Mock(returncode=0)
        proc.communicate = AsyncMock(return_value=(
            b'{"info":{"url":"https://r1.googlevideo.com/audio","resolver":"sender"}}', b""
        ))
        with patch("worker.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
            await resolve_sender("ZFZM6jDTWd4", {"ctt": "synthetic-secret", "unrelated": "omit"})
        self.assertEqual(spawn.call_args.args, ("node", "src/youtube-resolver.mjs"))
        payload = proc.communicate.call_args.args[0]
        self.assertIn(b"synthetic-secret", payload)
        self.assertNotIn(b"unrelated", payload)

    async def test_subprocess_is_killed_and_reaped_on_cancellation(self):
        proc = Mock(returncode=None)
        proc.wait = AsyncMock()
        started = asyncio.Event()

        async def communicate(_):
            started.set()
            await asyncio.Event().wait()

        proc.communicate = communicate
        with patch("worker.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(resolve_sender("ZFZM6jDTWd4", {"ctt": "synthetic"}))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
