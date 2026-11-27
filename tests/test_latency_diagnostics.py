import asyncio
import contextlib
import unittest
from unittest.mock import AsyncMock, Mock, patch

from worker import FRAME_BYTES, Worker, handle_command


class LatencyDiagnosticTests(unittest.IsolatedAsyncioTestCase):
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
