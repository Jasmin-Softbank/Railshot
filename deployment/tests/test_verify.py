"""Health-check control flow with fake Kubernetes and a real loopback HTTP server.

No root, cluster, external downloads, or third-party Python packages required.
"""
import http.server
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
MARKER = "Jasmin Deployment PoC OK"


class VerifyTests(unittest.TestCase):
    def run_verify(self, status=200, body=MARKER, internal_body=MARKER, cilium_code=0):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                k3s = root / "k3s"
                k3s.write_text("""#!/usr/bin/env bash
set -eu
args="$*"
case "$args" in
  *InternalIP*) printf 127.0.0.1 ;;
  *nodePort*) printf '%s' "$TEST_PORT" ;;
  *' exec '*) printf '%s' "$TEST_INTERNAL_BODY" ;;
  *) exit 0 ;;
esac
""")
                k3s.chmod(0o755)
                cilium = root / "cilium"
                cilium.write_text('#!/usr/bin/env bash\nexit "$TEST_CILIUM_CODE"\n')
                cilium.chmod(0o755)
                # Redirect only host binary locations in a temporary copy.
                common = (ROOT / "scripts/common.sh").read_text()
                (root / "common.sh").write_text(common.replace("/usr/local/bin/k3s", str(k3s)))
                verify = (ROOT / "scripts/verify.sh").read_text()
                (root / "verify.sh").write_text(
                    verify.replace("/usr/local/lib/jasmin-poc/cilium", str(cilium))
                )
                env = os.environ.copy()
                env.update(WAIT_TIMEOUT="1s", VERIFY_URL="", TEST_PORT=str(server.server_port),
                           TEST_INTERNAL_BODY=internal_body, TEST_CILIUM_CODE=str(cilium_code))
                return subprocess.run(["bash", str(root / "verify.sh")], env=env,
                                      text=True, capture_output=True, timeout=20)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_http_200_with_expected_body_passes(self):
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: HTTP 200", result.stdout)
        self.assertIn("외부 클라이언트", result.stdout)

    def test_http_200_from_wrong_application_fails(self):
        result = self.run_verify(body="Other application")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED: 단계=verify", result.stderr)
        self.assertNotIn("PASS:", result.stdout)

    def test_http_503_fails_even_with_expected_body(self):
        result = self.run_verify(status=503)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("마지막 HTTP=503", result.stderr)

    def test_internal_service_failure_blocks_nodeport_success(self):
        result = self.run_verify(internal_body="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("클러스터 내부 DNS/Service", result.stderr)
        self.assertNotIn("PASS:", result.stdout)

    def test_cilium_failure_preserves_exit_code_and_stage(self):
        result = self.run_verify(cilium_code=42)
        self.assertEqual(result.returncode, 42)
        self.assertIn("FAILED: 단계=verify, exit=42", result.stderr)
        self.assertNotIn("HTTP 확인:", result.stdout)


if __name__ == "__main__":
    unittest.main()
