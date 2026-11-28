import asyncio
import contextlib
import unittest
from unittest.mock import AsyncMock, Mock, patch

from worker import FRAME_BYTES, SILENCE, Worker


class ContinuousCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabling_continuous_mode_during_blocking_read_does_not_create_parallel_readers(self):
        worker = Worker(Mock(), {"stop_receiver_on_seek": True})
        entered, ready, audible = asyncio.Event(), asyncio.Event(), asyncio.Event()
        audio = bytes([1]) * FRAME_BYTES
        feeds = []
        reading = False

        async def read(_):
            nonlocal reading
            self.assertFalse(reading)
            reading = True
            entered.set()
            await ready.wait()
            reading = False
            return audio

        async def feed(pcm):
            feeds.append(pcm)
            if len(feeds) >= 4:
                audible.set()

        worker.decoder = Mock(
            stdout=Mock(readexactly=AsyncMock(side_effect=read), read=AsyncMock(return_value=b"")),
            returncode=0, wait=AsyncMock(return_value=0),
        )
        capture = Mock(start=AsyncMock(), feed=AsyncMock(side_effect=feed), stop=AsyncMock())
        worker.client = Mock(create_source_capture=Mock(return_value=capture))
        with patch("worker.emit"):
            task = worker.capture_task = asyncio.create_task(worker.stream())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await worker.command("configure_seek", {"stop_receiver_on_seek": False})
                self.assertTrue(worker.continuous_seek)
                ready.set()
                await asyncio.wait_for(audible.wait(), 1)
                self.assertTrue(all(pcm == audio for pcm in feeds))
                capture.start.assert_awaited_once()
                capture.stop.assert_not_awaited()
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await worker.stop_decoder()

    async def test_stalled_decoder_keeps_one_capture_clock_running_then_audio_at_full_rate(self):
        worker = Worker(Mock(), {"seek_without_output_stop": True})
        ready, silent, audible = asyncio.Event(), asyncio.Event(), asyncio.Event()
        feeds = []
        audio = bytes([1]) * FRAME_BYTES

        async def read(_):
            await ready.wait()
            return audio

        async def feed(pcm):
            feeds.append(pcm)
            if feeds.count(SILENCE) >= 3:
                silent.set()
            if feeds.count(audio) >= 4:
                audible.set()

        decoder = Mock(
            stdout=Mock(readexactly=AsyncMock(side_effect=read), read=AsyncMock(return_value=b"")),
            returncode=0, wait=AsyncMock(return_value=0),
        )
        worker.decoder = decoder
        capture = Mock(start=AsyncMock(), feed=AsyncMock(side_effect=feed), stop=AsyncMock())
        worker.client = Mock(create_source_capture=Mock(return_value=capture))
        with patch("worker.emit"):
            task = worker.capture_task = asyncio.create_task(worker.stream())
            try:
                await asyncio.wait_for(silent.wait(), 1)
                self.assertEqual(worker.position, 0)
                self.assertTrue(worker.streaming.is_set())
                capture.stop.assert_not_awaited()
                reader = worker.pcm_read_task
                await worker.command("configure_seek", {"stop_receiver_on_seek": True})
                self.assertIs(worker.pcm_read_task, reader)
                self.assertFalse(reader.done())
                await worker.command("configure_seek", {"stop_receiver_on_seek": False})
                ready.set()
                await asyncio.wait_for(audible.wait(), 1)
                # No alternating silence/audio from scheduling only one read
                # every two capture ticks.
                first = feeds.index(audio)
                self.assertTrue(all(pcm == audio for pcm in feeds[first:]))
                self.assertAlmostEqual(worker.position, feeds.count(audio) * 0.025)
                capture.start.assert_awaited_once()
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await worker.stop_decoder()

    async def test_stopping_decoder_cancels_pending_frame_read_without_stopping_capture(self):
        worker = Worker(Mock(), {"seek_without_output_stop": True})
        entered = asyncio.Event()

        async def read(_):
            entered.set()
            await asyncio.Event().wait()

        decoder = Mock(
            stdout=Mock(readexactly=AsyncMock(side_effect=read), read=AsyncMock(return_value=b"")),
            returncode=0, wait=AsyncMock(return_value=0),
        )
        worker.decoder = decoder
        capture = Mock(start=AsyncMock(), feed=AsyncMock(), stop=AsyncMock())
        worker.client = Mock(create_source_capture=Mock(return_value=capture))
        with patch("worker.emit") as emit:
            task = worker.capture_task = asyncio.create_task(worker.stream())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                reader = worker.pcm_read_task
                await asyncio.wait_for(worker.stop_decoder(), 1)
                self.assertTrue(reader.done())
                self.assertIsNone(worker.pcm_read_task)
                self.assertFalse(task.done())
                self.assertTrue(worker.streaming.is_set())
                capture.stop.assert_not_awaited()
                self.assertNotIn("TRACK_ENDED", [call.args[0] for call in emit.call_args_list])
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def test_stale_eof_cannot_advance_queue_or_feed_old_partial_frame(self):
        worker = Worker(Mock(), {})
        waiting, release, fed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def wait():
            waiting.set()
            await release.wait()
            return 0

        worker.decoder = Mock(
            stdout=Mock(readexactly=AsyncMock(side_effect=asyncio.IncompleteReadError(
                bytes([9]) * 100, FRAME_BYTES))),
            wait=AsyncMock(side_effect=wait),
        )
        capture = Mock(start=AsyncMock(), feed=AsyncMock(side_effect=lambda _: fed.set()),
                       stop=AsyncMock())
        worker.client = Mock(create_source_capture=Mock(return_value=capture))
        with patch("worker.emit") as emit:
            task = asyncio.create_task(worker.stream())
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                worker.invalidate()
                worker.decoder = None
                release.set()
                await asyncio.wait_for(fed.wait(), 1)
                self.assertNotIn("TRACK_ENDED", [call.args[0] for call in emit.call_args_list])
                self.assertTrue(all(call.args[0] == SILENCE for call in capture.feed.call_args_list))
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
