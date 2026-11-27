import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from routing import SessionRouter
from worker import Worker
from test_routing import FakeMA, URI


class OutputTransportTests(unittest.IsolatedAsyncioTestCase):
    async def worker(self):
        ma = FakeMA()
        worker = Worker(ma, {"target_player_id": "speaker"})
        worker.router = SessionRouter(ma, "speaker", URI)
        await worker.router.claim()
        worker.info = {"url": "https://example.test/audio"}
        worker.video_id = "ZFZM6jDTWd4"
        worker.duration = 500
        worker.position = 42
        worker.streaming.set()
        worker.stop_decoder = AsyncMock()

        async def decode(position):
            worker.position = position

        worker.decoder_at = AsyncMock(side_effect=decode)
        original_call = ma.call

        async def call(command, **args):
            await original_call(command, **args)
            if command == "players/cmd/stop":
                worker.streaming.clear()
            elif command == "players/cmd/select_source" and args["source"] == URI:
                worker.streaming.set()

        ma.call = call
        ma.calls.clear()
        return worker, ma

    async def test_pause_stops_output_but_preserves_session_position_and_power(self):
        worker, ma = await self.worker()
        result = await worker.command("pause", {})
        self.assertEqual(ma.calls, [("players/cmd/stop", {"player_id": "speaker"})])
        self.assertTrue(result["paused"])
        self.assertTrue(result["active"])
        self.assertEqual(result["position"], 42)
        self.assertIsNotNone(worker.info)
        self.assertTrue(worker.router.suspended)
        self.assertTrue(ma.p["powered"])
        worker.stop_decoder.assert_awaited_once()

    async def test_resume_reselects_source_and_decodes_saved_position(self):
        worker, ma = await self.worker()
        await worker.command("pause", {})
        ma.calls.clear()
        result = await worker.command("resume", {})
        self.assertEqual(ma.calls, [(
            "players/cmd/select_source", {"player_id": "speaker", "source": URI}
        )])
        worker.decoder_at.assert_awaited_once_with(42)
        self.assertFalse(result["paused"])
        self.assertFalse(worker.router.suspended)
        self.assertTrue(result["active"])

    async def test_duplicate_pause_and_resume_do_not_repeat_transport_commands(self):
        worker, ma = await self.worker()
        await worker.command("pause", {})
        await worker.command("pause", {})
        await worker.command("resume", {})
        await worker.command("resume", {})
        self.assertEqual([c for c, _ in ma.calls], [
            "players/cmd/stop", "players/cmd/select_source",
        ])
        worker.decoder_at.assert_awaited_once()

    async def test_seek_stops_output_before_restarting_at_new_position(self):
        worker, ma = await self.worker()
        result = await worker.command("seek", {"position": 120})
        self.assertEqual([c for c, _ in ma.calls], [
            "players/cmd/stop", "players/cmd/select_source",
        ])
        worker.decoder_at.assert_awaited_once_with(120)
        self.assertEqual(result["position"], 120)
        self.assertFalse(result["paused"])
        self.assertTrue(result["active"])
        self.assertTrue(ma.p["powered"])

    async def test_seek_while_paused_does_not_start_playback(self):
        worker, ma = await self.worker()
        await worker.command("pause", {})
        ma.calls.clear()
        result = await worker.command("seek", {"position": 120})
        self.assertEqual(ma.calls, [])
        worker.decoder_at.assert_not_awaited()
        self.assertEqual(result["position"], 120)
        self.assertTrue(result["paused"])
        await worker.command("resume", {})
        worker.decoder_at.assert_awaited_once_with(120)

    async def test_invalid_seek_does_not_stop_output(self):
        worker, ma = await self.worker()
        for position in [-1, 501, float("nan")]:
            with self.assertRaises(RuntimeError):
                await worker.command("seek", {"position": position})
        self.assertEqual(ma.calls, [])
        self.assertFalse(worker.router.suspended)

    async def test_resume_never_reclaims_another_source(self):
        worker, ma = await self.worker()
        await worker.command("pause", {})
        ma.p.update(active_source="other://music", playback_state="playing")
        ma.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "no longer owns"):
            await worker.command("resume", {})
        self.assertEqual(ma.calls, [])
        self.assertFalse(worker.router.active)
        self.assertTrue(ma.p["powered"])

    async def test_pause_and_seek_do_not_stop_another_source(self):
        for method, args in [("pause", {}), ("seek", {"position": 100})]:
            worker, ma = await self.worker()
            ma.p.update(active_source="other://music", playback_state="playing")
            with self.assertRaisesRegex(RuntimeError, "no longer owns"):
                await worker.command(method, args)
            self.assertEqual(ma.calls, [])
            self.assertFalse(worker.router.active)

    async def test_intentional_source_stop_does_not_end_casting(self):
        worker, ma = await self.worker()
        await worker.command("pause", {})
        await worker.source_released()
        self.assertTrue(worker.router.active)
        self.assertTrue(worker.router.suspended)
        self.assertIsNotNone(worker.info)

    async def test_external_source_change_while_paused_still_yields(self):
        worker, ma = await self.worker()
        await worker.command("pause", {})
        ma.p.update(active_source="other://music", playback_state="playing")
        ma.calls.clear()
        await worker.source_released()
        self.assertFalse(worker.router.active)
        self.assertEqual(ma.calls, [])

    async def test_old_release_callback_cannot_cancel_restarted_source(self):
        worker, ma = await self.worker()
        worker.source_epoch = 2
        worker.streaming.clear()
        await worker.source_released(epoch=1)
        self.assertTrue(worker.router.active)
        worker.stop_decoder.assert_not_awaited()

    async def test_external_stop_still_ends_casting(self):
        worker, ma = await self.worker()
        ma.p["playback_state"] = "idle"
        worker.streaming.clear()
        await worker.source_released()
        self.assertFalse(worker.router.active)

    async def test_finish_while_paused_restores_queue_and_releases_owned_power(self):
        worker, ma = await self.worker()
        await worker.command("pause", {})
        await worker.command("finish", {})
        self.assertFalse(worker.router.active)
        self.assertFalse(worker.router.suspended)
        self.assertFalse(ma.p["powered"])
        self.assertEqual(ma.p["active_source"], "speaker")
        self.assertIsNone(worker.info)

    async def test_resume_failure_stops_output_and_releases_session(self):
        worker, ma = await self.worker()
        await worker.command("pause", {})
        original_call = ma.call

        async def fail_select(command, **args):
            if command == "players/cmd/select_source" and args["source"] == URI:
                raise RuntimeError("Source could not restart")
            await original_call(command, **args)

        ma.call = fail_select
        with self.assertRaisesRegex(RuntimeError, "could not restart"):
            await worker.command("resume", {})
        self.assertFalse(worker.router.active)
        self.assertFalse(ma.p["powered"])
        self.assertEqual(ma.p["active_source"], "speaker")
        worker.decoder_at.assert_not_awaited()

    async def test_source_stop_start_callbacks_preserve_resumed_session(self):
        worker, ma = await self.worker()
        started = asyncio.Event()
        released = asyncio.Event()
        lifecycle = []

        async def stream():
            lifecycle.append("start")
            worker.streaming.set()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                lifecycle.append("end")
                worker.streaming.clear()

        original_release = worker.source_released

        async def release(epoch):
            try:
                await original_release(epoch)
            finally:
                released.set()

        worker.stream = stream
        worker.source_released = release
        original_call = ma.call

        async def call(command, **args):
            await original_call(command, **args)
            if command == "players/cmd/stop":
                worker.source_command(SimpleNamespace(source=SimpleNamespace(command="stop")))
            elif command == "players/cmd/select_source" and args["source"] == URI:
                worker.source_command(SimpleNamespace(source=SimpleNamespace(command="start")))

        ma.call = call
        worker.source_command(SimpleNamespace(source=SimpleNamespace(command="start")))
        await asyncio.wait_for(started.wait(), 1)
        try:
            await worker.command("pause", {})
            await worker.command("resume", {})
            await asyncio.wait_for(released.wait(), 1)
            self.assertEqual(lifecycle, ["start", "end", "start"])
            self.assertTrue(worker.router.active)
            self.assertFalse(worker.paused)
            self.assertTrue(worker.streaming.is_set())
        finally:
            await worker.stop_capture()
