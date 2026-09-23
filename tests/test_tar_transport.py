import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from minio import Minio

from lidar_hd.snowball import UploadReader, upload_tar


class TarTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.archive = Path(temp.name) / "tileset.tar"
        self.archive.write_bytes(b"tar fixture" * 10000)
        self.client = Minio("localhost:9000", access_key="test", secret_key="test",
                            session_token="session-test", region="us-east-1")
        self.client._http = Mock()
        self.client._http.urlopen.return_value = Mock(status=200)

    def test_tls_signed_metadata_token_and_single_put_above_5_gib(self):
        # Exercise the header/transport bound without allocating a 6 GiB fixture.
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=6 * 1024 ** 3)):
            upload_tar(self.client, "bucket", self.archive, Mock(), lambda: False)
        args, kwargs = self.client._http.urlopen.call_args
        self.assertEqual(args[0], "PUT")
        self.assertEqual(kwargs["headers"]["Content-Length"], str(6 * 1024 ** 3))
        self.assertEqual(kwargs["headers"]["x-amz-content-sha256"], "UNSIGNED-PAYLOAD")
        self.assertEqual(kwargs["headers"]["X-Amz-Security-Token"], "session-test")
        self.assertIn("x-amz-security-token", kwargs["headers"]["Authorization"])
        self.assertIn("x-amz-meta-snowball-auto-extract", kwargs["headers"]["Authorization"])
        self.assertIsInstance(kwargs["body"], UploadReader)
        self.assertFalse(kwargs["retries"])
        self.assertFalse(kwargs["redirect"])

    def test_http_error_closes_response(self):
        response = Mock(status=413)
        self.client._http.urlopen.return_value = response
        with self.assertRaisesRegex(RuntimeError, "HTTP 413"):
            upload_tar(self.client, "bucket", self.archive, Mock(), lambda: False)
        response.close.assert_called_once()
        response.release_conn.assert_called_once()

    def test_cancel_before_and_during_stream(self):
        with self.assertRaises(InterruptedError):
            upload_tar(self.client, "bucket", self.archive, Mock(), lambda: True)
        self.client._http.urlopen.assert_not_called()
        stopped = False
        def consume(*args, **kwargs):
            nonlocal stopped
            self.assertTrue(kwargs["body"].read(16))
            stopped = True
            kwargs["body"].read(16)
        self.client._http.urlopen.side_effect = consume
        with self.assertRaises(InterruptedError):
            upload_tar(self.client, "bucket", self.archive, Mock(), lambda: stopped)

    def test_reader_bounds_memory_and_reports_final_bytes(self):
        data = b"x" * (2 * 1024 * 1024 + 3)
        events = []
        reader = UploadReader(io.BytesIO(data), len(data), lambda *args: events.append(args), lambda: False)
        chunks = []
        while chunk := reader.read():
            self.assertLessEqual(len(chunk), 1024 * 1024)
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), data)
        self.assertEqual(events[-1], (len(data), len(data)))

    def test_real_urllib3_sends_exact_stream_without_chunked_encoding(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                size = int(self.headers["Content-Length"])
                received.append((self.headers, self.rfile.read(size)))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Minio(f"127.0.0.1:{server.server_port}", access_key="test",
                           secret_key="test", secure=False, region="us-east-1")
            events = []
            upload_tar(client, "bucket", self.archive, lambda *args: events.append(args), lambda: False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(len(received), 1)
        headers, data = received[0]
        self.assertEqual(data, self.archive.read_bytes())
        self.assertIsNone(headers.get("Transfer-Encoding"))
        self.assertEqual(headers["X-Amz-Meta-Snowball-Auto-Extract"], "true")
        self.assertEqual(events[0], (0, len(data)))
        self.assertEqual(events[-1], (len(data), len(data)))


if __name__ == "__main__":
    unittest.main()