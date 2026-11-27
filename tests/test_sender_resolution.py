import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from routing import ManualSession
from worker import Worker


class SenderResolutionTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, **config):
        worker = Worker(Mock(), {"routing_mode": "manual", **config})
        worker.router = ManualSession("sendspin_source://audio_source/test")
        return worker

    async def test_sender_is_default_and_context_reaches_resolver(self):
        worker = self.worker()
        info = {"url": "https://r1.googlevideo.com/audio", "resolver": "sender"}
        with patch.object(worker.resolver, "resolve", AsyncMock(return_value=info)) as sender:
            with patch("worker.resolve") as anonymous:
                result = await worker.resolve_media("ZFZM6jDTWd4", {"ctt": "synthetic"})
        sender.assert_awaited_once_with("ZFZM6jDTWd4", {"ctt": "synthetic"})
        anonymous.assert_not_called()
        self.assertEqual(result["resolver"], "sender")

    async def test_failure_never_falls_back_by_default(self):
        for config in ({}, {"allow_anonymous_fallback": False}, {"allow_anonymous_fallback": "true"}):
            worker = self.worker(**config)
            with patch.object(worker.resolver, "resolve", AsyncMock(side_effect=RuntimeError("SECRET"))):
                with patch("worker.resolve") as anonymous:
                    with self.assertRaisesRegex(RuntimeError, "Sender-authorized audio unavailable"):
                        await worker.resolve_media("ZFZM6jDTWd4", {})
            anonymous.assert_not_called()

    async def test_explicit_fallback_is_labelled_and_logged(self):
        worker = self.worker(allow_anonymous_fallback=True)
        with patch.object(worker.resolver, "resolve", AsyncMock(side_effect=RuntimeError)):
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

        with patch.object(worker.resolver, "resolve", slow):
            task = asyncio.create_task(worker.play("ZFZM6jDTWd4", context={"ctt": "synthetic"}))
            await asyncio.wait_for(started.wait(), 1)
            await worker.command("finish", {})
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(worker.router.active)
        self.assertIsNone(worker.info)

if __name__ == "__main__":
    unittest.main()
