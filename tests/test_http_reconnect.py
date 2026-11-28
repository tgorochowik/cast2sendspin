"""Real FFmpeg against synthetic local audio; never contacts YouTube or MA."""
import asyncio
import re
import shutil
import socket
import struct
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from worker import FFMPEG_HTTP_OPTIONS


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed for the HTTP reconnect test")
class HTTPReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_resumes_at_byte_offset_and_preserves_all_pcm_without_looping_at_eof(self):
        # Deterministic three-second audio; compare every decoded byte, not just
        # duration, to detect replayed, dropped, or duplicated input.
        pcm = struct.pack("<48000h", *(i % 32768 for i in range(48000))) * 6
        data = pcm
        requests = []
        cutoff = 131072

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                match = re.fullmatch(r"bytes=(\d+)-", self.headers.get("Range", ""))
                offset = int(match[1]) if match else 0
                requests.append(offset)
                self.send_response(206 if match else 200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data) - offset))
                if match:
                    self.send_header("Content-Range", f"bytes {offset}-{len(data) - 1}/{len(data)}")
                self.end_headers()
                if len(requests) == 1:
                    self.wfile.write(data[offset:cutoff])
                    self.wfile.flush()
                    # An actual TCP reset with an unfulfilled Content-Length.
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.close_connection = True
                else:
                    self.wfile.write(data[offset:])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proc = None
        try:
            for options in ((), FFMPEG_HTTP_OPTIONS):
                with self.subTest(reconnect=bool(options)):
                    requests.clear()
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
                        *options,
                        "-f", "s16le", "-ar", "48000", "-ac", "2",
                        "-i", f"http://127.0.0.1:{server.server_port}/audio",
                        "-vn", "-ac", "2", "-ar", "48000", "-f", "s16le", "pipe:1",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    output, diagnostic = await asyncio.wait_for(proc.communicate(), 12)
                    self.assertEqual(proc.returncode, 0, diagnostic.decode())
                    if not options:
                        # Reproduce the bug: truncated output and a clean exit.
                        self.assertLess(len(output), len(pcm))
                        continue
                    self.assertEqual(output, pcm)
                    # RST can discard in-flight bytes; continuation must use
                    # the number actually received, possibly less than cutoff.
                    self.assertTrue(any(0 < offset <= cutoff for offset in requests), requests)
                    self.assertLessEqual(len(requests), 4)
                    self.assertIn(b"Will reconnect", diagnostic)
        finally:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
