"""Run the release workflow's shell against a local bare Git remote, never GHCR/K3s."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKLOAD = "gitops/applications/railshot-platform/workload.json"
REFERENCE = "refs/heads/deployment/platform"


class PlatformReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="railshot-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "source"
        self.repo.mkdir()
        self.remote = self.home / "remote.git"
        self.artifacts = self.home / "platform-published"
        self.artifacts.mkdir()
        for name in ("deployment/scripts/publish-platform.py", "deployment/scripts/render-platform.py",
                     "deployment/manifests/platform.yaml"):
            destination = self.repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        (self.repo / "README.md").write_text("reviewed source\n")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", ".")
        self.git("commit", "-m", "reviewed source")
        self.source_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("init", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "main")
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/platform-publish.yml").read_text())
        self.write_images("a")

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.repo, check=check, text=True, capture_output=True)

    def write_images(self, digest):
        for name in ("dashboard", "api"):
            (self.artifacts / f"{name}.json").write_text(json.dumps({
                name: f"ghcr.io/jasmin-softbank/railshot-{name}@sha256:{digest * 64}"}))

    def run_step(self, job, name, overrides=None):
        step = next(step for step in self.workflow["jobs"][job]["steps"] if step.get("name") == name)
        env = {**os.environ, "RUNNER_TEMP": str(self.home), "GITHUB_SHA": self.source_sha,
               "GITHUB_REF": "refs/heads/integration/test", "PUBLISH": "true", "DEPLOY": "true",
               "GITHUB_REPOSITORY": "Jasmin-Softbank/Railshot", "GITHUB_REPOSITORY_ID": "1400202256",
               "GITHUB_OUTPUT": str(self.home / "github-output"), "VERIFY_REF": "refs/heads/integration/test",
               "VERIFY_ROLE": "arn:aws:iam::721622471953:role/railshot-platform-verifier",
               "VERIFY_VERSION": "1", "VERIFY_HASH": "e" * 64,
               "COMPONENTS": '["dashboard","api"]', "PLATFORM_TARGET": "k3s-aws", "PLATFORM_PORT": "31080",
               **(overrides or {})}
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.repo,
                              env=env, text=True, capture_output=True)

    def deploy(self):
        return self.run_step("deploy", "Commit the tested digest declaration to the platform branch")

    def remote_revision(self):
        result = self.git("ls-remote", "--exit-code", "origin", REFERENCE, check=False)
        return result.stdout.split()[0] if result.returncode == 0 else None

    def test_workflow_admission_and_opt_in_controls(self):
        dispatch = self.workflow.get("on", self.workflow.get(True))["workflow_dispatch"]["inputs"]
        self.assertIs(dispatch["deploy"]["default"], False)
        self.assertEqual(self.workflow["concurrency"], {"group": "platform-release", "cancel-in-progress": False})
        deploy = self.workflow["jobs"]["deploy"]
        self.assertEqual(deploy["if"], "inputs.deploy && inputs.publish")
        self.assertIn("publish", deploy["needs"])
        self.assertEqual(deploy["permissions"], {"contents": "write", "actions": "read"})
        name = "Validate publication and deployment inputs"
        self.assertEqual(self.run_step("admission", name).returncode, 0)
        for overrides in ({"PUBLISH": "false"}, {"COMPONENTS": '["dashboard"]'},
                          {"PLATFORM_TARGET": ""}, {"PLATFORM_PORT": ""}, {"PLATFORM_PORT": "443"},
                          {"GITHUB_REF": "refs/heads/feature/unreviewed"},
                          {"COMPONENTS": '["dashboard","api","api"]'}, {"VERIFY_REF": "refs/heads/main"},
                          {"VERIFY_HASH": ""}, {"VERIFY_ROLE": ""}, {"GITHUB_REPOSITORY_ID": "1"}):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self.run_step("admission", name, overrides).returncode, 0)
        self.assertIsNone(self.remote_revision())
        verification = self.workflow["jobs"]["verify"]
        self.assertEqual(verification["needs"], "deploy")
        self.assertEqual(verification["permissions"], {"contents": "read", "id-token": "write"})

    def test_release_creates_reviewed_branch_and_preserves_its_existing_files(self):
        first = self.deploy()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_revision = self.remote_revision()
        self.assertEqual(json.loads(first.stdout)["revision"], first_revision)
        outputs = (self.home / "github-output").read_text()
        self.assertIn(f"revision={first_revision}\n", outputs)
        self.assertIn(f"dashboard_digest={'a' * 64}\n", outputs)
        self.assertIn(f"api_digest={'a' * 64}\n", outputs)
        self.assertEqual(self.git("rev-parse", f"{first_revision}^").stdout.strip(), self.source_sha)
        self.assertEqual(self.git("diff", "--name-only", self.source_sha, first_revision).stdout.strip(), WORKLOAD)

        # A newer source tree must not replace independently maintained branch files.
        self.git("switch", "main")
        (self.repo / "README.md").write_text("new source, not a config branch update\n")
        self.git("commit", "-am", "new reviewed source")
        self.source_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "--detach", first_revision)
        (self.repo / "operator-note.txt").write_text("preserve this branch file\n")
        self.git("add", "operator-note.txt")
        self.git("commit", "-m", "operator branch change")
        operator_revision = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("push", "origin", f"HEAD:{REFERENCE}")
        self.git("switch", "--detach", self.source_sha)
        self.write_images("b")
        second = self.deploy()
        self.assertEqual(second.returncode, 0, second.stderr)
        second_revision = self.remote_revision()
        self.assertEqual(self.git("rev-parse", f"{second_revision}^").stdout.strip(), operator_revision)
        self.assertEqual(self.git("diff", "--name-only", operator_revision, second_revision).stdout.strip(), WORKLOAD)
        self.assertEqual(self.git("show", f"{second_revision}:operator-note.txt").stdout, "preserve this branch file\n")
        self.assertEqual(self.git("show", f"{second_revision}:README.md").stdout, "reviewed source\n")
        workload = json.loads(self.git("show", f"{second_revision}:{WORKLOAD}").stdout)
        deployments = [item for item in workload["items"] if item["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 2)
        for item in deployments:
            self.assertTrue(item["spec"]["template"]["spec"]["containers"][0]["image"].endswith("b" * 64))
        self.git("switch", "--detach", self.source_sha)
        unchanged = self.deploy()
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
        self.assertEqual(json.loads(unchanged.stdout)["status"], "unchanged")
        self.assertEqual(self.remote_revision(), second_revision)

    def test_untrusted_or_missing_digest_fails_before_branch_creation(self):
        image = self.artifacts / "api.json"
        image.write_text(json.dumps({"api": "ghcr.io/jasmin-softbank/railshot-api:latest"}))
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertIsNone(self.remote_revision())
        image.unlink()
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertIsNone(self.remote_revision())

    def test_initial_branch_is_created_even_when_source_already_contains_declaration(self):
        images = self.home / "images.json"
        images.write_text(json.dumps({name: json.loads((self.artifacts / f"{name}.json").read_text())[name]
                                     for name in ("dashboard", "api")}))
        rendered = subprocess.run(["python3", "deployment/scripts/render-platform.py", str(images),
                                   "--target-id", "k3s-aws", "--dashboard-node-port", "31080"],
                                  cwd=self.repo, check=True, text=True, capture_output=True)
        path = self.repo / WORKLOAD
        path.parent.mkdir(parents=True)
        path.write_text(rendered.stdout)
        self.git("add", WORKLOAD)
        self.git("commit", "-m", "reviewed declaration already present")
        self.source_sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_revision(), self.source_sha)
        self.assertEqual(json.loads(result.stdout)["status"], "published")

    def test_rejected_push_does_not_claim_success_or_change_remote(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"status": "published"', result.stdout)
        self.assertIsNone(self.remote_revision())


if __name__ == "__main__":
    unittest.main()
