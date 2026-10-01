"""Real local HTTP responses must influence the load probe's exit status."""
import http.server
import json
from pathlib import Path
import subprocess
import threading
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/load-http.py"


class LoadTests(unittest.TestCase):
    def probe(self, status, body, allow_failures=False):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                args = ["python3", str(SCRIPT), f"http://127.0.0.1:{server.server_port}/",
                        "--requests", "17", "--concurrency", "4"]
                if allow_failures:
                    args.append("--allow-failures")
                result = subprocess.run(args, text=True, capture_output=True, timeout=10)
                return result.returncode, json.loads(result.stdout)
            finally:
                server.shutdown()
                worker.join()

    def test_all_requests_have_correct_body(self):
        code, stats = self.probe(200, b"Jasmin Deployment PoC OK\n")
        self.assertEqual(code, 0)
        self.assertEqual(stats["requests"], 17)
        self.assertEqual(stats["failures"], 0)

    def test_wrong_application_returns_failure(self):
        code, stats = self.probe(200, b"Wrong app")
        self.assertEqual(code, 1)
        self.assertEqual(stats["failures"], 17)

    def test_http_500_is_counted_even_when_disruption_is_expected(self):
        code, stats = self.probe(500, b"Jasmin Deployment PoC OK", allow_failures=True)
        self.assertEqual(code, 0)
        self.assertEqual(stats["failures"], 17)
        self.assertEqual(stats["error_rate"], 1)


if __name__ == "__main__":
    unittest.main()
