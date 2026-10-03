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
               "SKIP_BUILD": "false", "AUTO_RELEASE": "false", "MULTICLOUD": "false", "MULTICLOUD_ENABLED": "",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ID": "123", "CI_RUN_ID": "123",
               "GITHUB_REPOSITORY": "Jasmin-Softbank/Railshot", "GITHUB_REPOSITORY_ID": "1400202256",
               "GITHUB_OUTPUT": str(self.home / "github-output"), "VERIFY_REF": "refs/heads/integration/test",
               "VERIFY_ROLE": "arn:aws:iam::721622471953:role/railshot-platform-verifier",
               "VERIFY_VERSION": "1", "VERIFY_HASH": "e" * 64,
               "RELEASE_VERSION": "", "RELEASE_HASH": "",
               "COMPONENTS": '["dashboard","api"]', "PLATFORM_TARGET": "k3s-aws", "PLATFORM_PORT": "31080",
               "PROVIDER_TARGETS": "{}",
               **(overrides or {})}
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.repo,
                              env=env, text=True, capture_output=True)

    def deploy(self, overrides=None):
        return self.run_step("deploy", "Commit the tested digest declaration to the platform branch", overrides)

    def remote_revision(self):
        result = self.git("ls-remote", "--exit-code", "origin", REFERENCE, check=False)
        return result.stdout.split()[0] if result.returncode == 0 else None

    def test_workflow_admission_and_opt_in_controls(self):
        dispatch = self.workflow.get("on", self.workflow.get(True))["workflow_dispatch"]["inputs"]
        self.assertIs(dispatch["deploy"]["default"], False)
        self.assertEqual(self.workflow["concurrency"], {"group": "platform-release", "cancel-in-progress": False})
        deploy = self.workflow["jobs"]["deploy"]
        self.assertEqual(deploy["if"], "${{ always() && !cancelled() && inputs.deploy && inputs.publish && needs.admission.result == 'success' && needs.publish.result == 'success' }}")
        self.assertIn("publish", deploy["needs"])
        self.assertEqual(deploy["permissions"], {"contents": "write", "actions": "read"})
        for job, name in [('admission', 'Validate publication and deployment inputs'),
                          ('deploy', 'Commit the tested digest declaration to the platform branch')]:
            step = next(step for step in self.workflow['jobs'][job]['steps'] if step.get('name') == name)
            self.assertEqual(step['env']['PROVIDER_TARGETS'], "${{ vars.RAILSHOT_PROVIDER_TARGETS || '{}' }}")
        name = "Validate publication and deployment inputs"
        self.assertEqual(self.run_step("admission", name).returncode, 0)
        for overrides in ({"PUBLISH": "false"}, {"COMPONENTS": '["dashboard"]'},
                          {"PLATFORM_TARGET": ""}, {"PLATFORM_PORT": ""}, {"PLATFORM_PORT": "443"},
                          {"GITHUB_REF": "refs/heads/feature/unreviewed"},
                          {"COMPONENTS": '["dashboard","api","api"]'}, {"VERIFY_REF": "refs/heads/main"},
                          {"VERIFY_HASH": ""}, {"VERIFY_ROLE": ""}, {"GITHUB_REPOSITORY_ID": "1"},
                          *({'PROVIDER_TARGETS': value} for value in ('bad-json', 'null', '[]', '{"aws":"replacement"}',
                                                                     '{"openstack":"k3s-aws"}', '{"unknown":"k3s-unknown"}'))):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self.run_step("admission", name, overrides).returncode, 0)
        self.assertIsNone(self.remote_revision())
        verification = self.workflow["jobs"]["verify"]
        self.assertEqual(verification['if'], "${{ always() && !cancelled() && inputs.deploy && inputs.publish && needs.deploy.result == 'success' }}")
        self.assertEqual(verification["needs"], "deploy")
        self.assertEqual(verification["permissions"], {"contents": "read", "id-token": "write"})

    def test_same_run_reuse_requires_opted_in_trusted_complete_image_set(self):
        triggers = self.workflow.get('on', self.workflow.get(True))
        self.assertIs(triggers['workflow_call']['inputs']['skip_build']['default'], False)
        self.assertNotIn('skip_build', triggers['workflow_dispatch']['inputs'])
        jobs = self.workflow['jobs']
        self.assertEqual(jobs['build']['if'], '${{ !inputs.skip_build }}')
        self.assertEqual(jobs['publish']['needs'], ['admission', 'build'])
        self.assertEqual(jobs['publish']['if'], "${{ always() && !cancelled() && inputs.publish && needs.admission.result == 'success' && (needs.build.result == 'success' || (inputs.skip_build && needs.build.result == 'skipped')) }}")
        self.assertEqual(jobs['multicloud']['needs'], ['deploy', 'verify', 'ci-runtime'])
        self.assertEqual(jobs['multicloud']['if'], "${{ always() && !cancelled() && inputs.multicloud && vars.RAILSHOT_MULTICLOUD_RELEASE == 'true' && inputs.deploy && inputs.publish && needs.deploy.result == 'success' && needs.verify.result == 'success' && needs.ci-runtime.result == 'success' }}")
        admission = next(step for step in jobs['admission']['steps'] if step.get('name') == 'Require the exact trusted source CI gate before deployment')
        self.assertEqual(admission['env']['CI_RUN_ID'], '${{ inputs.ci_run_id }}')
        self.assertIn('release_admission.py', admission['run'])
        for job in ('publish', 'deploy', 'ci-runtime', 'multicloud'):
            downloads = [step for step in jobs[job]['steps'] if step.get('uses', '').startswith('actions/download-artifact@')]
            self.assertEqual(len(downloads), 1)
            self.assertFalse(set(downloads[0]['with']) & {'run-id', 'repository', 'github-token'}, 'reuse must read the calling run artifacts')
        publish = next(step['run'] for step in jobs['publish']['steps'] if step.get('name') == 'Publish exactly the tested image')
        self.assertNotIn('docker build', publish)
        self.assertIn('org.opencontainers.image.revision', publish)
        self.assertIn('= "$GITHUB_SHA"', publish)
        valid = {'SKIP_BUILD': 'true', 'AUTO_RELEASE': 'true', 'MULTICLOUD': 'true', 'MULTICLOUD_ENABLED': 'true',
                 'GITHUB_EVENT_NAME': 'push', 'COMPONENTS': '["dashboard","api","mcp","ci-runner"]',
                 'RELEASE_VERSION': '2', 'RELEASE_HASH': 'f' * 64,
                 'PROVIDER_TARGETS': '{"aws":"k3s-aws","gcp":"k3s-gcp","openstack":"k3s-openstack"}'}
        name = 'Validate publication and deployment inputs'
        result = self.run_step('admission', name, valid)
        self.assertEqual(result.returncode, 0, result.stderr)
        for changes in ({'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_EVENT_NAME': 'workflow_dispatch'},
                        {'GITHUB_REF': 'refs/heads/integration/other'}, {'AUTO_RELEASE': 'false'},
                        {'CI_RUN_ID': '124'}, {'PUBLISH': 'false'}, {'DEPLOY': 'false'},
                        {'COMPONENTS': '["dashboard","api","ci-runner"]'}, {'GITHUB_REPOSITORY_ID': '1'},
                        {'PROVIDER_TARGETS': '{"aws":"k3s-aws","gcp":"k3s-gcp"}'}):
            with self.subTest(changes=changes):
                self.assertNotEqual(self.run_step('admission', name, {**valid, **changes}).returncode, 0)

    def test_platform_automatic_release_does_not_require_multicloud_activation(self):
        triggers = self.workflow.get('on', self.workflow.get(True))
        for trigger in ('workflow_call', 'workflow_dispatch'):
            self.assertIs(triggers[trigger]['inputs']['multicloud']['default'], False)
        ci = yaml.safe_load((ROOT / '.github/workflows/railshot-ci.yml').read_text())
        release = ci['jobs']['release']
        self.assertEqual(release['with']['multicloud'], "${{ vars.RAILSHOT_MULTICLOUD_RELEASE == 'true' }}")
        self.assertIn("vars.RAILSHOT_AUTO_RELEASE == 'true'", release['if'])
        self.assertNotIn('RAILSHOT_MULTICLOUD_RELEASE', release['if'])
        runtime = self.workflow['jobs']['ci-runtime']
        self.assertEqual(runtime['needs'], ['deploy', 'verify'])
        self.assertNotIn('multicloud', runtime['if'])
        self.assertIn("contains(fromJSON(inputs.components), 'ci-runner')", runtime['if'])
        self.assertTrue(any('--scope ci-runtime' in step.get('run', '') for step in runtime['steps']))
        automatic = {'SKIP_BUILD': 'true', 'AUTO_RELEASE': 'true', 'GITHUB_EVENT_NAME': 'push',
                     'RELEASE_VERSION': '2', 'RELEASE_HASH': 'f' * 64,
                     'COMPONENTS': '["dashboard","api","mcp","ci-runner"]'}
        name = 'Validate publication and deployment inputs'
        result = self.run_step('admission', name, automatic)
        self.assertEqual(result.returncode, 0, result.stderr)
        multicloud = {**automatic, 'MULTICLOUD': 'true', 'MULTICLOUD_ENABLED': 'true', 'RELEASE_VERSION': '2', 'RELEASE_HASH': 'f' * 64,
                      'PROVIDER_TARGETS': '{"aws":"k3s-aws","gcp":"k3s-gcp","openstack":"k3s-openstack"}'}
        for missing in ({'PROVIDER_TARGETS': '{}'}, {'RELEASE_VERSION': ''}, {'RELEASE_HASH': ''}):
            with self.subTest(missing=missing):
                self.assertNotEqual(self.run_step('admission', name, {**multicloud, **missing}).returncode, 0)

    def test_every_entrypoint_requires_repository_multicloud_activation(self):
        name = 'Validate publication and deployment inputs'
        step = next(step for step in self.workflow['jobs']['admission']['steps'] if step.get('name') == name)
        self.assertEqual(step['env']['MULTICLOUD_ENABLED'], '${{ vars.RAILSHOT_MULTICLOUD_RELEASE }}')
        for event, skip in (('workflow_dispatch', 'false'), ('push', 'true')):
            for enabled in ('true', 'false', '', 'TRUE'):
                for requested in ('true', 'false'):
                    with self.subTest(event=event, enabled=enabled, requested=requested):
                        settings = {'GITHUB_EVENT_NAME': event, 'SKIP_BUILD': skip, 'AUTO_RELEASE': 'true',
                            'MULTICLOUD': requested, 'MULTICLOUD_ENABLED': enabled,
                            'COMPONENTS': '["dashboard","api","mcp","ci-runner"]',
                            'PROVIDER_TARGETS': '{"aws":"k3s-aws","gcp":"k3s-gcp","openstack":"k3s-openstack"}',
                            'RELEASE_VERSION': '2', 'RELEASE_HASH': 'f' * 64}
                        result = self.run_step('admission', name, settings)
                        allowed = requested == 'false' or enabled == 'true'
                        self.assertEqual(result.returncode == 0, allowed, result.stderr)
                        if not allowed:
                            self.assertIn('RAILSHOT_MULTICLOUD_RELEASE=true', result.stderr)

    def test_current_source_gate_is_rechecked_immediately_before_each_mutation(self):
        for job, mutation in (('deploy', 'Commit the tested digest declaration to the platform branch'),
                              ('ci-runtime', 'Promote the tested CI controller runner and workflow source'),
                              ('multicloud', 'Apply the same approved release and require three verified providers')):
            steps = self.workflow['jobs'][job]['steps']
            index = next(i for i, step in enumerate(steps) if step.get('name') == mutation)
            guard = steps[index - 1]
            self.assertEqual(guard['env'], {'GITHUB_TOKEN': '${{ github.token }}', 'CI_RUN_ID': '${{ inputs.ci_run_id }}'})
            self.assertEqual(guard['run'], 'python3 deployment/scripts/release_admission.py --source-sha "$GITHUB_SHA" --ref "$GITHUB_REF" --ci-run-id "$CI_RUN_ID"')
            self.assertNotIn('continue-on-error', guard)
            self.assertEqual(self.workflow['jobs'][job]['permissions']['actions'], 'read')
        remote = next(step for step in self.workflow['jobs']['multicloud']['steps']
                      if step.get('name') == 'Apply the same approved release and require three verified providers')
        self.assertIn('--publication-run-id "$GITHUB_RUN_ID" --publication-run-attempt "$GITHUB_RUN_ATTEMPT"', remote['run'])

    def test_release_keeps_enabled_provider_targets_across_image_updates(self):
        settings = {'PROVIDER_TARGETS': '{"openstack":"k3s-openstack"}'}
        self.assertEqual(self.run_step('admission', 'Validate publication and deployment inputs', settings).returncode, 0)
        revisions = []
        for digest in ('a', 'b'):
            self.git('switch', '--detach', self.source_sha)
            self.write_images(digest)
            result = self.deploy(settings)
            self.assertEqual(result.returncode, 0, result.stderr)
            revisions.append(self.remote_revision())
            workload = json.loads(self.git('show', f'{revisions[-1]}:{WORKLOAD}').stdout)
            api = next(item for item in workload['items'] if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-api')
            container = api['spec']['template']['spec']['containers'][0]
            self.assertTrue(container['image'].endswith(digest * 64))
            env = {item['name']: item.get('value') for item in container['env']}
            self.assertEqual(env['RAILSHOT_TARGET_ID'], 'k3s-aws')
            self.assertEqual(env['RAILSHOT_TARGET_PROVIDER'], 'aws')
            self.assertEqual(json.loads(env['RAILSHOT_PROVIDER_TARGETS']), {'openstack': 'k3s-openstack'})
            self.assertEqual(env['RAILSHOT_TARGET_IDS'], 'k3s-aws,k3s-openstack')
        self.assertEqual(self.git('rev-parse', f'{revisions[1]}^').stdout.strip(), revisions[0])
        self.assertEqual(self.git('diff', '--name-only', *revisions).stdout.strip(), WORKLOAD)

    def test_invalid_provider_map_blocks_release_before_branch_creation(self):
        for value in ('bad-json', 'null', '[]', '{"openstack":"k3s-aws"}'):
            with self.subTest(provider_targets=value):
                self.assertNotEqual(self.deploy({'PROVIDER_TARGETS': value}).returncode, 0)
                self.assertIsNone(self.remote_revision())

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
