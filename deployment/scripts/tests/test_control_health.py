#!/usr/bin/env python3
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deployment/scripts/control-health.sh"


def service(service, state="running", health="healthy"):
    return {"Service": service, "State": state, "Health": health}


class ControlHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = pathlib.Path(self.temp.name)
        self.env_file = self.directory / "control.env"
        self.env_file.write_text("CONTROL_VAULT_IMAGE=example@sha256:test\n", encoding="utf-8")
        self.env_file.chmod(0o600)
        self.bin_dir = self.directory / "bin"
        self.bin_dir.mkdir()
        fake_docker = self.bin_dir / "docker"
        fake_docker.write_text("#!/bin/sh\nprintf '%s' \"$FAKE_COMPOSE_PS\"\n", encoding="utf-8")
        fake_docker.chmod(0o700)
        self.environment = os.environ | {"PATH": f"{self.bin_dir}:{os.environ['PATH']}"}

    def tearDown(self):
        self.temp.cleanup()

    def run_health(self, rows, *, line_delimited=False):
        payload = "\n".join(json.dumps(row) for row in rows) if line_delimited else json.dumps(rows)
        return subprocess.run([str(SCRIPT), str(self.env_file)], cwd=ROOT, env=self.environment | {"FAKE_COMPOSE_PS": payload}, text=True, capture_output=True)

    def test_accepts_healthy_array_and_line_delimited_status(self):
        rows = [service("vault"), service("custody")]
        self.assertEqual(self.run_health(rows).returncode, 0)
        self.assertEqual(self.run_health(rows, line_delimited=True).returncode, 0)

    def test_rejects_sealed_stopped_missing_and_unhealthy_services(self):
        cases = {
            "sealed": [service("vault", health="unhealthy"), service("custody")],
            "stopped": [service("vault", state="exited"), service("custody")],
            "missing": [service("vault")],
            "unhealthy": [service("vault"), service("custody", health="unhealthy")],
        }
        for name, rows in cases.items():
            with self.subTest(name=name):
                self.assertNotEqual(self.run_health(rows).returncode, 0)

    def test_rejects_group_readable_and_symlink_env_files(self):
        self.env_file.chmod(0o640)
        self.assertEqual(self.run_health([service("vault"), service("custody")]).returncode, 65)
        self.env_file.chmod(0o600)
        target = self.directory / "target.env"
        target.write_text(self.env_file.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o600)
        self.env_file.unlink()
        self.env_file.symlink_to(target)
        self.assertEqual(self.run_health([service("vault"), service("custody")]).returncode, 65)


if __name__ == "__main__":
    unittest.main()
