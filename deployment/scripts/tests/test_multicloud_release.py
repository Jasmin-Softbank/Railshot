"""Exercise release fanout and promotion with no cloud credentials or mutations."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deployment/scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


release = load('multicloud_release')
admission = load('release_admission')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.manifest = {'version': 1, 'source_sha': 'a' * 40, 'platform_revision': 'b' * 40,
            'images': {name: 'ghcr.io/jasmin-softbank/railshot-' + name + '@sha256:' + 'c' * 64 for name in release.COMPONENTS},
            'runtime_policy': json.loads((ROOT / 'deployment/airgap/versions.json').read_text()),
            'edge_kinds': {p: 'native' for p in release.PROVIDERS},
            'edge_modules': {provider: 'd' * 64 for provider in release.PROVIDERS},
            'provider_targets': {provider: 'k3s-' + provider for provider in release.PROVIDERS}}
        self.config = {'version': 1, 'state_dir': str(Path(self.directory.name).resolve()), 'workers': {}, 'apps': {}, 'operator_kubeconfig': release.OPERATOR_STATE + 'control-kubeconfig',
            'gcp_credentials_file': release.OPERATOR_STATE + 'gcp-wif.json', 'targets': [
            {'provider': provider, 'target_id': 'k3s-' + provider,
             **{name: release.OPERATOR_STATE + 'config/' + provider + '/' + name for name in
                ('registry_file', 'config_file', 'registration_state', 'from_policy_file', 'edge_config_file')}} for provider in sorted(release.PROVIDERS)]}
        self.promotions = []
        self.workers = SimpleNamespace(apply_workers=lambda *args: {'status': 'verified', 'executable_verification': True}, verify_workers=lambda *args: {'status':'verified', 'executable_verification': True}, verify_apps=lambda *args: {'status':'verified'})

    def run_release(self, target):
        def promote(*args):
            self.promotions.append(args[2]); return {'status': 'verified'}
        return release.execute(self.config, self.manifest, workers=self.workers, target_runner=target,
                               platform_verify=lambda *args: {'status': 'cluster_verified'}, promote=promote,
                               target_verifier=lambda row, manifest: self.proof(row))

    def proof(self, target, status='verified'):
        return {'provider': target['provider'], 'target_id': target['target_id'], 'source_sha': self.manifest['source_sha'], 'release_sha': self.manifest['source_sha'], 'status': status}

    def test_parallel_three_target_success_promotes_once_and_replay_is_read_only(self):
        barrier = threading.Barrier(3)
        def target(row, manifest):
            barrier.wait(timeout=5); return self.proof(row)
        result = self.run_release(target)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual({t['provider'] for t in result['targets']}, release.PROVIDERS)
        self.assertEqual(len(self.promotions), 1)
        again = self.run_release(lambda *args: self.fail('mutation replayed'))
        self.assertEqual(again['status'], 'verified'); self.assertIn('reverified_at', again)

    def test_one_failure_keeps_all_results_and_never_promotes_or_retries(self):
        result = self.run_release(lambda target, manifest: self.proof(target, 'failed' if target['provider'] == 'gcp' else 'verified'))
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(len(result['targets']), 3)
        self.assertFalse(self.promotions)
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION_REQUIRED'):
            self.run_release(lambda *args: self.fail('failed mutation replayed'))

    def test_misbound_receipt_cannot_satisfy_three_provider_gate(self):
        result = self.run_release(lambda target, manifest: {**self.proof(target), 'source_sha': 'f' * 40})
        self.assertEqual(result['status'], 'incomplete'); self.assertFalse(self.promotions)

    def test_target_program_rejects_misbound_runtime_receipt_without_promoting_baseline(self):
        temporary = Path(self.directory.name).resolve()
        target = copy.deepcopy(self.config['targets'][0])
        target['from_policy_file'] = str(temporary / 'from-policy.json')
        target['edge_config_file'] = str(temporary / 'edge-config.json')
        before = {'previous_policy': True}
        release.save(Path(target['from_policy_file']), before)
        release.save(Path(target['edge_config_file']), {'edge_kind': 'native'})
        edge_proof = {'phase': 'succeeded', 'provider': target['provider'], 'release_sha': self.manifest['source_sha']}
        runtime_proof = {**self.proof(target), 'provider': 'gcp'}
        replies = [SimpleNamespace(returncode=0, stdout=json.dumps(edge_proof)),
                   SimpleNamespace(returncode=0, stdout=json.dumps(runtime_proof))]
        real_path = Path
        fixed_state = '/home/railshot-operator/.local/share/railshot/multicloud-releases'
        def redirected_path(value, *parts):
            return real_path(temporary / 'target-state' if str(value) == fixed_state else value, *parts)
        output = io.StringIO()
        payload = {'target': target, 'release': self.manifest, 'source_root': str(ROOT)}
        # Execute the unchanged production program, replacing only its external processes
        # and fixed operator state root. durable_write still writes real private temp files.
        with mock.patch.dict(sys.modules, {'pathlib': SimpleNamespace(Path=redirected_path)}), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, 'stdout', output), \
                mock.patch.object(sys, 'path', list(sys.path)), \
                mock.patch('subprocess.run', side_effect=replies) as run, \
                self.assertRaises(SystemExit) as stopped:
            exec(compile(release.TARGET_PROGRAM, '<target-program>', 'exec'), {})
        self.assertEqual(stopped.exception.code, 1)
        proof = json.loads(output.getvalue())
        self.assertEqual(proof['status'], 'failed')
        self.assertEqual(proof['code'], 'RUNTIME_RECEIPT_BINDING_MISMATCH')
        self.assertEqual(json.loads(real_path(target['from_policy_file']).read_text()), before)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(real_path(run.call_args_list[0].args[0][1]).name, 'edge_update.py')
        self.assertEqual(real_path(run.call_args_list[1].args[0][1]).name, 'runtime-update.py')

    def test_new_source_cannot_bypass_prior_uncertain_release(self):
        self.assertEqual(self.run_release(lambda row, _: self.proof(row, 'unknown'))['status'], 'incomplete')
        self.manifest['source_sha'] = 'e' * 40
        with self.assertRaisesRegex(ValueError, 'PRIOR_RELEASE_RECONCILIATION_REQUIRED'):
            self.run_release(lambda *args: self.fail('new source bypassed unresolved release'))
        self.assertFalse(self.promotions)

    def test_missing_provider_or_mutated_pin_rejected_before_workers(self):
        for field in ('targets', 'policy'):
            with self.subTest(field=field):
                config, manifest = copy.deepcopy(self.config), copy.deepcopy(self.manifest)
                if field == 'targets': config['targets'].pop()
                else: manifest['runtime_policy']['runtime']['k3s_version'] = 'latest'
                with self.assertRaises(ValueError): release.validate(manifest, config)

    def test_verified_release_requires_live_health_and_cannot_replay_after_new_attempt(self):
        self.run_release(lambda row, _: self.proof(row))
        with self.assertRaisesRegex(ValueError, 'LIVE_READBACK_FAILED'):
            release.execute(self.config, self.manifest, workers=self.workers,
                platform_verify=lambda *args: {}, target_verifier=lambda row, _: self.proof(row, 'failed'))
        release.save(Path(self.config['state_dir']) / 'last-attempt.json', {'input_sha256': 'later-incomplete'})
        with self.assertRaisesRegex(ValueError, 'SUPERSEDED_RECONCILIATION_REQUIRED'):
            self.run_release(lambda *args: self.fail('mutation replayed'))

    def test_host_program_compiles_and_tool_download_rejects_wrong_checksum(self):
        compile(release.TARGET_PROGRAM, '<target>', 'exec')
        tools = load('bootstrap-release-tools')
        body = b'tested tool bytes'
        self.assertEqual(tools.checked(body, tools.hashlib.sha256(body).hexdigest()), body)
        with self.assertRaisesRegex(ValueError, 'CHECKSUM_MISMATCH'):
            tools.checked(body, '0' * 64)


class AdmissionTests(unittest.TestCase):
    def test_requires_current_sha_trusted_push_latest_attempt_gate(self):
        sha = 'a' * 40
        run = {'id': 12, 'run_attempt': 2, 'head_sha': sha, 'head_branch': 'main', 'head_repository': {'full_name': admission.REPOSITORY},
               'path': '.github/workflows/railshot-ci.yml', 'event': 'push', 'conclusion': None}
        jobs = {'total_count': 1, 'jobs': [{'name': 'Railshot CI gate', 'status': 'completed', 'conclusion': 'success'}]}
        def read(path):
            if path == 'git/ref/heads/main': return {'object': {'sha': sha}}
            if path == 'actions/runs/12': return run
            self.assertEqual(path, 'actions/runs/12/attempts/2/jobs?per_page=100'); return jobs
        self.assertEqual(admission.admit(sha, 'refs/heads/main', 12, read=read)['ci_run_attempt'], 2)
        for change in ({'event': 'pull_request'}, {'head_sha': 'b' * 40}, {'conclusion': 'failure'}):
            old = dict(run); run.update(change)
            with self.assertRaises(ValueError): admission.admit(sha, 'refs/heads/main', 12, read=read)
            run.clear(); run.update(old)
        jobs['jobs'][0]['conclusion'] = 'failure'
        with self.assertRaises(ValueError): admission.admit(sha, 'refs/heads/main', 12, read=read)


if __name__ == '__main__':
    unittest.main()
