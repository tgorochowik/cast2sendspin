import unittest
from unittest.mock import AsyncMock, Mock, patch

from worker import Worker


class DecoderDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_stderr_classifies_truncation_and_extracts_only_safe_statistics(self):
        worker = Worker(Mock(), {})
        decoder = Mock(stderr=Mock(read=AsyncMock(side_effect=[
            b"https://example.test/signed-secret Stream ends pre",
            b"maturely\n" + b"x" * 4096,
            b"\nFile ended prematurely\nConnection reset by peer\n",
            b"Duration: 00:03:36.01, start: 0.000000\n",
            b"[AVIOContext @ 0x1234] Statistics: 6300123 bytes read, 0 seeks\n",
            b"",
        ])))
        with patch("worker.emit") as emit:
            await worker.read_decoder_diagnostics(decoder, 3)
        self.assertEqual(
            [call.kwargs["reason"] for call in emit.call_args_list
             if call.args == ("DECODER_DIAGNOSTIC",)],
            ["http_truncated", "container_truncated", "connection_reset"],
        )
        self.assertEqual(emit.call_args.args, ("DECODER_INPUT_STATS",))
        self.assertEqual(emit.call_args.kwargs, {
            "generation": 3, "bytes_read": 6300123, "input_seeks": 0,
            "container_duration": 216.01,
        })
        self.assertNotIn("signed-secret", repr(emit.call_args_list))
        self.assertTrue(all(call.args == (4096,) for call in decoder.stderr.read.call_args_list))

    async def test_unrecognized_messages_are_never_forwarded(self):
        worker = Worker(Mock(), {})
        decoder = Mock(stderr=Mock(read=AsyncMock(side_effect=[
            b"Unexpected upstream exception: secret\n", b"",
        ])))
        with patch("worker.emit") as emit:
            await worker.read_decoder_diagnostics(decoder, 1)
        emit.assert_called_once_with("DECODER_INPUT_STATS", generation=1)
