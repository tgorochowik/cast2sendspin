import asyncio
import contextlib
import sys
import unittest
from unittest.mock import AsyncMock, Mock

from worker import Worker


class DecoderShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_drains_buffered_output_without_waiting_for_kill_timeout(self):
        # A small reader limit reproduces an idle FFmpeg whose PCM pipe is full.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import os, signal\n"
            "def finish(*_):\n"
            "    os.write(1, b'final audio' * 65536)\n"
            "    os._exit(0)\n"
            "signal.signal(signal.SIGTERM, finish)\n"
            "os.write(1, b'x' * 4096)\n"
            "os.write(2, b'ready\\n')\n"
            "while True:\n"
            "    os.write(1, b'x' * 4096)\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024,
        )
        worker = Worker(Mock(), {})
        worker.decoder = proc
        try:
            await asyncio.wait_for(proc.stderr.readline(), 2)
            await asyncio.wait_for(proc.stdout.readexactly(1), 2)
            await asyncio.wait_for(worker.stop_decoder(), 1)
            self.assertIsNone(worker.decoder)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.communicate()

    async def test_stop_without_decoder_is_a_noop(self):
        worker = Worker(Mock(), {})
        await worker.stop_decoder()
        self.assertIsNone(worker.decoder)

    async def test_drain_waits_for_existing_pcm_reader(self):
        worker = Worker(Mock(), {})
        terminated = asyncio.Event()
        stdout = Mock(read=AsyncMock(side_effect=[b"buffered audio", b""]))
        proc = Mock(
            stdout=stdout, returncode=None, wait=AsyncMock(),
            terminate=Mock(side_effect=terminated.set),
        )
        worker.decoder = proc
        async with worker.decoder_read_lock:
            task = asyncio.create_task(worker.stop_decoder())
            await asyncio.wait_for(terminated.wait(), 1)
            stdout.read.assert_not_awaited()
        await asyncio.wait_for(task, 1)
        self.assertEqual(stdout.read.await_count, 2)
        proc.wait.assert_awaited_once()
        proc.kill.assert_not_called()
