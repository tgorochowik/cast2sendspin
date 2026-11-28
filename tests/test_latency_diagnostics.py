import asyncio
import contextlib
import unittest
from unittest.mock import AsyncMock, Mock, patch

from worker import FRAME_BYTES, Worker, handle_command


class LatencyDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_exit_with_missing_audio_is_error_not_queue_advance(self):
        worker = Worker(Mock(), {})
        worker.position, worker.duration = 203.65, 216
        worker.info = {"audio_duration": 215.741}
        worker.decoder = Mock(
            stdout=Mock(readexactly=AsyncMock(side_effect=asyncio.IncompleteReadError(b"", FRAME_BYTES))),
            wait=AsyncMock(return_value=0),
        )
        fed = asyncio.Event()
        capture = Mock(start=AsyncMock(), feed=AsyncMock(side_effect=lambda _: fed.set()),
                       stop=AsyncMock())
        worker.client = Mock(create_source_capture=Mock(return_value=capture))
        with patch("worker.emit") as emit:
            task = asyncio.create_task(worker.stream())
            try:
                await asyncio.wait_for(fed.wait(), 1)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        events = [call.args[0] for call in emit.call_args_list]
        self.assertIn("PLAYBACK_ERROR", events)
        self.assertNotIn("TRACK_ENDED", events)

    async def test_decoder_eof_records_position_duration_and_exit_without_media_secrets(self):
        for code in (0, 1):
            with self.subTest(returncode=code):
                worker = Worker(Mock(), {})
                worker.position, worker.duration = 239, 240
                worker.video_id = "ZFZM6jDTWd4"
                worker.info = {"url": "https://example.test/signed-url-secret"}
                worker.decoder = Mock(
                    stdout=Mock(readexactly=AsyncMock(side_effect=asyncio.IncompleteReadError(b"x", FRAME_BYTES))),
                    wait=AsyncMock(return_value=code),
                )
                fed = asyncio.Event()
                capture = Mock(start=AsyncMock(), feed=AsyncMock(side_effect=lambda _: fed.set()),
                               stop=AsyncMock())
                worker.client = Mock(create_source_capture=Mock(return_value=capture))
                with patch("worker.emit") as emit:
                    task = asyncio.create_task(worker.stream())
                    try:
                        await asyncio.wait_for(fed.wait(), 1)
                    finally:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                events = {call.args[0]: call.kwargs for call in emit.call_args_list}
                eof = events["DECODER_EOF"]
                self.assertEqual(eof["position"], 239)
                self.assertEqual(eof["duration"], 240)
                self.assertEqual(eof["returncode"], code)
                self.assertEqual(eof["partial_bytes"], 1)
                self.assertTrue(eof["current_decoder"])
                self.assertIn("TRACK_ENDED" if code == 0 else "PLAYBACK_ERROR", events)
                self.assertNotIn("signed-url-secret", repr(emit.call_args_list))

    async def test_command_timings_do_not_log_arguments(self):
        worker = Mock(command=AsyncMock(return_value={"paused": True}))
        with patch("worker.emit") as emit:
            await handle_command(worker, {
                "id": 7, "method": "pause",
                "args": {"context": {"ctt": "synthetic-secret"}},
            })
        received, applied, result = emit.call_args_list
        self.assertEqual(received.args, ("COMMAND_RECEIVED",))
        self.assertEqual(applied.args, ("COMMAND_APPLIED",))
        self.assertEqual(applied.kwargs["request_id"], 7)
        self.assertGreaterEqual(applied.kwargs["duration_ms"], 0)
        self.assertEqual(result.args, ("RESULT",))
        self.assertNotIn("synthetic-secret", repr(emit.call_args_list))

    async def test_command_failure_only_logs_exception_type(self):
        worker = Mock(command=AsyncMock(side_effect=RuntimeError("signed-url-secret")))
        with patch("worker.emit") as emit:
            await handle_command(worker, {"id": 8, "method": "seek"})
        self.assertEqual(emit.call_args.kwargs["error"], "RuntimeError")
        self.assertNotIn("signed-url-secret", repr(emit.call_args_list))

    async def test_pause_logs_transition_when_silence_reaches_capture(self):
        worker = Worker(Mock(), {})
        worker.decoder = Mock(stdout=Mock(
            readexactly=AsyncMock(return_value=bytes(FRAME_BYTES)),
        ))
        paused = asyncio.Event()
        transitions = []

        async def feed(_):
            worker.paused = True

        def emit(event, **fields):
            if event == "PCM_TRANSITION":
                transitions.append(fields["kind"])
                if fields["kind"] == "paused":
                    paused.set()

        capture = Mock(start=AsyncMock(), feed=AsyncMock(side_effect=feed), stop=AsyncMock())
        worker.client = Mock(create_source_capture=Mock(return_value=capture))
        with patch("worker.emit", side_effect=emit):
            task = asyncio.create_task(worker.stream())
            try:
                await asyncio.wait_for(paused.wait(), 1)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.assertEqual(transitions, ["audio", "paused"])
