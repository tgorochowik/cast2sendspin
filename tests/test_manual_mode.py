import asyncio
import contextlib
import struct
import unittest
from unittest.mock import AsyncMock, Mock, patch

from routing import ManualSession
from worker import FRAME_BYTES, Worker, apply_input_volume


class ManualModeTests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        ma = Mock()
        ma.call = AsyncMock(side_effect=AssertionError("Must not control MA outputs"))
        ma.player = AsyncMock(side_effect=AssertionError("Must not select an MA output"))
        worker = Worker(ma, {"routing_mode": "manual"})
        worker.router = ManualSession("sendspin_source://audio_source/manual")
        worker.volume = {"level": 100, "muted": False}
        return worker

    async def test_manual_start_never_looks_up_or_controls_an_output(self):
        worker = self.worker()
        worker.ma.client.disconnect = AsyncMock()
        client = Mock(connected=True)
        client.disconnect = AsyncMock()
        with patch("worker.connect_source", AsyncMock(return_value=(client, "source"))), patch.object(
            worker.resolver, "warmup", AsyncMock()
        ):
            await worker.start()
            await worker.close()
        worker.ma.player.assert_not_awaited()
        worker.ma.call.assert_not_awaited()

    async def test_play_accepts_track_before_input_is_selected_without_decoding(self):
        worker = self.worker()
        worker.decoder_at = AsyncMock()
        with patch.object(worker.resolver, "resolve", AsyncMock(return_value={
            "url": "https://example.test/audio", "duration": 100, "resolver": "sender"
        })):
            result = await asyncio.wait_for(worker.play("Kf5pXDhx5Vc", 12), 1)
        self.assertTrue(result["active"])
        self.assertEqual(result["position"], 12)
        self.assertTrue(worker.decoder_pending)
        worker.decoder_at.assert_not_awaited()
        await worker.command("finish", {})
        worker.ma.call.assert_not_awaited()
        self.assertFalse(worker.router.active)

    async def test_seek_before_routing_is_deferred(self):
        worker = self.worker()
        await worker.router.claim()
        worker.duration = 100
        worker.info = {"url": "https://example.test/audio"}
        worker.decoder_at = AsyncMock()
        await worker.command("seek", {"position": 30})
        self.assertEqual(worker.position, 30)
        self.assertTrue(worker.decoder_pending)
        worker.decoder_at.assert_not_awaited()
        worker.ma.call.assert_not_awaited()

    async def test_volume_changes_input_not_speaker_even_before_playback(self):
        worker = self.worker()
        await worker.command("volume", {"level": 50, "muted": False})
        self.assertEqual(worker.volume["level"], 50)
        worker.ma.call.assert_not_awaited()

    async def test_pause_resume_and_seek_never_control_manual_outputs(self):
        worker = self.worker()
        await worker.router.claim()
        worker.duration = 100
        worker.info = {"url": "https://example.test/audio"}
        await worker.command("pause", {})
        await worker.command("seek", {"position": 30})
        self.assertTrue(worker.paused)
        await worker.command("resume", {})
        self.assertFalse(worker.paused)
        worker.ma.call.assert_not_awaited()
        worker.ma.player.assert_not_awaited()

    async def test_ma_source_release_does_not_end_youtube_session(self):
        worker = self.worker()
        await worker.router.claim()
        worker.info = {"url": "https://example.test/audio"}
        await worker.source_released()
        self.assertTrue(worker.router.active)
        self.assertTrue(worker.decoder_pending)
        worker.ma.call.assert_not_awaited()

    async def test_audio_begins_only_when_ma_requests_source_stream(self):
        worker = self.worker()
        await worker.router.claim()
        worker.info = {"url": "https://example.test/audio"}
        worker.decoder_pending = True
        capture = Mock(start=AsyncMock(), feed=AsyncMock(), stop=AsyncMock())
        worker.client = Mock(create_source_capture=Mock(return_value=capture))

        async def decode(position):
            stdout = Mock(
                readexactly=AsyncMock(return_value=bytes(FRAME_BYTES)),
                read=AsyncMock(return_value=b""),
            )
            worker.decoder = Mock(stdout=stdout, returncode=0, wait=AsyncMock())
            worker.position = position

        worker.decoder_at = AsyncMock(side_effect=decode)
        self.assertEqual(worker.position, 0)
        worker.capture_task = asyncio.create_task(worker.stream())
        try:
            async with asyncio.timeout(1):
                while worker.position == 0:
                    await asyncio.sleep(0.01)
        finally:
            worker.capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker.capture_task
        worker.decoder_at.assert_awaited_once()
        self.assertGreater(worker.position, 0)
        capture.feed.assert_awaited()
        worker.ma.call.assert_not_awaited()

    def test_input_gain_and_mute(self):
        pcm = struct.pack("<3h", 12000, -12000, 0)
        self.assertEqual(apply_input_volume(pcm, {"level": 100, "muted": False}), pcm)
        self.assertEqual(apply_input_volume(pcm, {"level": 100, "muted": True}), bytes(6))
        self.assertEqual(
            struct.unpack("<3h", apply_input_volume(pcm, {"level": 50, "muted": False})),
            (4243, -4243, 0),
        )


if __name__ == "__main__":
    unittest.main()
