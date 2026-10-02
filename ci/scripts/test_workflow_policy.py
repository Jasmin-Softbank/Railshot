"""Regression checks for CI publication and the CD handoff; no cloud access."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest

import yaml

HERE = Path(__file__).resolve().parent


class WorkflowPolicyTest(unittest.TestCase):
    def test_registry_target_and_credentials_belong_only_to_trusted_release(self):
        jobs = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']
        release = jobs['release']
        self.assertEqual(release['environment'], 'railshot-release')
        self.assertEqual(release['env']['REGISTRY_PREFIX'], '${{ vars.REGISTRY_PREFIX }}')
        self.assertEqual(release['env']['REGISTRY_VISIBILITY'], "${{ vars.REGISTRY_VISIBILITY || 'private' }}")
        self.assertEqual(release['permissions'], {'contents': 'read', 'packages': 'write'})
        self.assertNotIn('REGISTRY_', json.dumps(jobs['loop']))
        login = next(s for s in release['steps'] if s.get('name') == 'Log in to GHCR')
        self.assertEqual(login['env'], {'GHCR_TOKEN': '${{ secrets.GITHUB_TOKEN }}'})
        self.assertEqual(sum('secrets.GITHUB_TOKEN' in json.dumps(s) for s in release['steps']), 1)
        scripts = '\n'.join(s.get('run', '') for s in release['steps'])
        self.assertIn('"$REGISTRY_PREFIX/${TENANT}-${APP}"', scripts)
        self.assertIn('docker login ghcr.io', scripts)
        self.assertNotIn('REGISTRY_PASSWORD', json.dumps(release))
        self.assertNotIn('REGISTRY_USERNAME', json.dumps(release))
        self.assertNotIn('GITOPS_TOKEN', json.dumps(release))

    def test_registry_binding_rejects_ambiguous_or_source_controlled_targets(self):
        release = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']['release']
        script = next(s['run'] for s in release['steps'] if s.get('name') == 'Validate trusted platform bindings')
        env = {**os.environ, 'PLATFORM_REF': 'a' * 40, 'REGISTRY_VISIBILITY': 'public'}
        for prefix in ('ghcr.io/owner', 'ghcr.io/owner/project/nested'):
            with self.subTest(prefix=prefix):
                result = subprocess.run(['bash', '-c', script], env={**env, 'REGISTRY_PREFIX': prefix}, capture_output=True)
                self.assertEqual(result.returncode, 0)
        for prefix in ('', 'owner/project', 'https://ghcr.io/owner', 'ghcr.io/owner/',
                       'user:password@ghcr.io/owner', '--help', 'ghcr.io/owner; false',
                       'ghcr.io/owner\nother/project', 'ghcr.io/../project', 'harbor.example.test/project', 'ghcr.io:443/owner'):
            with self.subTest(prefix=prefix):
                result = subprocess.run(['bash', '-c', script], env={**env, 'REGISTRY_PREFIX': prefix}, capture_output=True)
                self.assertNotEqual(result.returncode, 0)

    def test_private_default_blocks_before_registry_login_and_cannot_use_ready_flag(self):
        release = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']['release']
        guard = release['steps'][0]
        self.assertEqual(guard['name'], 'Validate trusted platform bindings')
        env = {**os.environ, 'PLATFORM_REF': 'a' * 40, 'REGISTRY_PREFIX': 'ghcr.io/owner', 'REGISTRY_READY': 'true', 'IMAGE_PULL_SECRET_READY': 'true'}
        for visibility in ('', 'private', 'true', 'PUBLIC'):
            with self.subTest(visibility=visibility):
                result = subprocess.run(['bash', '-c', guard['run']], env={**env, 'REGISTRY_VISIBILITY': visibility}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('BLOCKED:', result.stderr)
        env.pop('REGISTRY_VISIBILITY', None)
        self.assertNotEqual(subprocess.run(['bash', '-c', guard['run']], env=env, capture_output=True).returncode, 0)

    def test_login_uses_password_stdin_and_private_ephemeral_config(self):
        release = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']['release']
        login = next(s for s in release['steps'] if s.get('name') == 'Log in to GHCR')
        cleanup = next(s for s in release['steps'] if s.get('name') == 'Remove ephemeral registry credentials')
        self.assertEqual(cleanup['if'], 'always()')
        self.assertNotIn('runner.', json.dumps(release['env']))
        prepare = next(s for s in release['steps'] if s.get('name') == 'Set registry credential path')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, 'CAPTURE': tmp, 'RUNNER_TEMP': tmp,
                   'GITHUB_ENV': str(root / 'env'), 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
                   'REGISTRY_PREFIX': 'ghcr.io/owner',
                   'GITHUB_ACTOR': 'workflow-actor', 'GHCR_TOKEN': 'synthetic-test-secret'}
            subprocess.run(['bash', '-c', prepare['run']], env=env, check=True)
            self.assertEqual((root / 'env').read_text(), f'DOCKER_CONFIG={tmp}/railshot-registry-123-2\n')
            env['DOCKER_CONFIG'] = str(root / 'railshot-registry-123-2')
            # This shell function captures the actual workflow's argv/stdin without contacting a registry.
            stub = 'docker() { printf "%s\\n" "$@" > "$CAPTURE/argv"; cat > "$CAPTURE/stdin"; }\n'
            result = subprocess.run(['bash', '-c', stub + login['run']], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'argv').read_text().splitlines(),
                             ['login', 'ghcr.io', '--username', 'workflow-actor', '--password-stdin'])
            self.assertEqual((root / 'stdin').read_text(), env['GHCR_TOKEN'])
            self.assertNotIn(env['GHCR_TOKEN'], result.stdout + result.stderr + (root / 'argv').read_text())
            self.assertEqual(Path(env['DOCKER_CONFIG']).stat().st_mode & 0o777, 0o700)
            subprocess.run(['bash', '-c', cleanup['run']], env=env, check=True)
            self.assertFalse(Path(env['DOCKER_CONFIG']).exists())

    def test_public_mode_requires_each_digest_without_inherited_docker_credentials(self):
        release = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']['release']
        check = next(s for s in release['steps'] if s.get('name') == 'Require anonymous access to published digests')
        steps = release['steps']
        self.assertLess(steps.index(check), next(i for i,s in enumerate(steps) if s.get('id') == 'published'))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inherited = root / 'authenticated'; inherited.mkdir(); (inherited / 'config.json').write_text('{"auths":{"ghcr.io":{"auth":"synthetic-only"}}}')
            refs = {'web': 'ghcr.io/owner/tenant-app-web@sha256:' + 'a' * 64,
                    'api': 'ghcr.io/owner/tenant-app-api@sha256:' + 'b' * 64}
            (root / 'images.json').write_text(json.dumps(refs))
            env = {**os.environ, 'RUNNER_TEMP': tmp, 'REGISTRY_PREFIX': 'ghcr.io/owner',
                   'DOCKER_CONFIG': str(inherited), 'DOCKER_AUTH_CONFIG': 'synthetic-inherited-auth',
                   'CAPTURE': tmp, 'PRIVATE_DIGEST': ''}
            stub = '''docker() {
              test "$DOCKER_CONFIG" != "$CAPTURE/authenticated" && test -z "$DOCKER_AUTH_CONFIG" || return 90
              test "$(cat "$DOCKER_CONFIG/config.json")" = '{"auths":{"ghcr.io":{}}}' || return 91
              printf '%s\\n' "$*" >> "$CAPTURE/calls"
              test "$3" != "$PRIVATE_DIGEST"
            }
            '''
            result = subprocess.run(['bash', '-c', stub + check['run']], cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'calls').read_text().splitlines(), ['manifest inspect ' + v for v in refs.values()])
            self.assertFalse(list(root.glob('railshot-anonymous.*')))
            result = subprocess.run(['bash', '-c', stub + check['run']], cwd=tmp,
                                    env={**env, 'PRIVATE_DIGEST': refs['api']}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not anonymously readable', result.stderr)
            self.assertFalse(list(root.glob('railshot-anonymous.*')))
            (root / 'images.json').write_text('{}')
            self.assertNotEqual(subprocess.run(['bash', '-c', stub + check['run']], cwd=tmp, env=env, capture_output=True).returncode, 0)

    def test_submitted_commit_and_operator_target_match_before_any_agent_work(self):
        jobs = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']
        guard = next(step for step in jobs['loop']['steps'] if step.get('name') == 'Validate inputs')
        env = {**os.environ, 'TENANT': 'demo', 'APP': 'my-app', 'SOURCE_COMMIT': 'a' * 40,
               'GITHUB_SHA': 'a' * 40, 'CHECKOUT_SHA': 'a' * 40, 'TARGET_ID': 'aws-demo', 'CONFIGURED_TARGET': 'aws-demo'}
        stub = 'git() { test "$*" = "rev-parse HEAD" || return 9; printf "%s\\n" "$CHECKOUT_SHA"; }\n'
        self.assertEqual(subprocess.run(['bash', '-c', stub + guard['run']], env=env, capture_output=True).returncode, 0)
        for change in ({'GITHUB_SHA': 'b' * 40}, {'CHECKOUT_SHA': 'b' * 40}, {'SOURCE_COMMIT': 'main'},
                       {'TARGET_ID': 'onprem-demo'}, {'CONFIGURED_TARGET': ''}, {'APP': 'ab'}, {'TENANT': 'team-demo'}):
            with self.subTest(change=change):
                self.assertNotEqual(subprocess.run(['bash', '-c', stub + guard['run']], env={**env, **change}, capture_output=True).returncode, 0)

    def test_publication_handoff_contains_bound_evidence_without_deployment(self):
        text = (HERE.parent / 'workflows/railshot-deploy.yml').read_text()
        jobs = yaml.safe_load(text)['jobs']
        self.assertEqual(set(jobs), {'loop', 'release'})
        release = jobs['release']
        self.assertEqual(release['needs'], 'loop')
        self.assertEqual(release['outputs']['bundle_id'], '${{ needs.loop.outputs.bundle_id }}')
        self.assertEqual(release['outputs']['published_id'], '${{ steps.published.outputs.artifact-id }}')
        handoff = next(step for step in release['steps'] if step.get('id') == 'published')
        self.assertEqual(handoff['with']['name'], 'published-${{ github.run_attempt }}')
        self.assertEqual(handoff['with']['path'], 'published')
        receipt = next(step for step in release['steps'] if step.get('name') == 'Bind publication to the source and target')
        self.assertEqual(receipt['env']['BUNDLE_ARTIFACT_ID'], '${{ needs.loop.outputs.bundle_id }}')
        self.assertIn('publication.py release-bundle images.json published', receipt['run'])
        downloaded = next(step for step in release['steps'] if step.get('uses', '').startswith('actions/download-artifact'))
        self.assertEqual(downloaded['with']['artifact-ids'], '${{ needs.loop.outputs.bundle_id }}')
        scripts = '\n'.join(step.get('run', '') for step in release['steps'])
        self.assertIn('bundle.py publish release-bundle', scripts)
        self.assertNotIn('docker build', scripts)
        for removed in ('GITOPS_', 'RAILSHOT_DOMAIN', 'STORAGE_CLASS', 'render.py', 'render.json', 'kubectl', 'git push', 'KEDA', 'AUTOSCALING_PROFILE'):
            self.assertNotIn(removed, text)
        self.assertIn('exit 1', next(s['run'] for s in jobs['loop']['steps'] if s.get('id') == 'loop').split('passed=false')[1])


if __name__ == '__main__':
    unittest.main()
