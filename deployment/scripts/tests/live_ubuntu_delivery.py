#!/usr/bin/env python3
"""Explicit disposable-VM drill using the actual delivery executor and SSH transport.

Requires an already approved target, private synthetic request/config and sample
workload. Mutates only that registered application and its delivery credential.
Never prints credential values. Does not exercise the product API or GitOps.
"""
import argparse
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import secrets_delivery as delivery


def observe(target, app, plain, protected):
    with delivery.Executor(target, app) as executor:
        assertion = 'IFS= read -r expected; test "$GREETING" = "$expected"; '
        if protected is None:
            assertion += 'test "${API_TEST_TOKEN+x}" != x'
        else:
            assertion += 'IFS= read -r expected; test "$API_TEST_TOKEN" = "$expected"'
        remote = shlex.join(['sudo', '-n', 'k3s', 'kubectl', '-n', app['namespace'],
                             'exec', '-i', 'deployment/' + app['deployment'], '--', 'sh', '-ceu', assertion])
        result = subprocess.run([*executor.prefix, remote], input=plain + '\n' + (protected or '') + '\n',
                                text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('pod environment comparison failed; no value printed')


def run(config_path, request_path, output):
    config = delivery.private_json(Path(config_path)); initial = delivery.private_json(Path(request_path))
    delivery.validate_request(initial)
    if initial['project_id'] != 'canary-project' or initial['app'] != 'railshot-secret-canary':
        raise ValueError('only the explicit disposable canary fixture is supported')
    target, app = delivery.select_target(config, initial)
    checks = []
    def record(check, **fields):
        checks.append({'check': check, 'passed': True, **fields})
        delivery.atomic_private(Path(output), {'environment_id': initial['environment_id'], 'checks': checks})
        print(json.dumps(checks[-1]), flush=True)
    def submit(request):
        for phase in ('prepare', 'apply', 'verify'):
            result = delivery.deliver(config, {**request, 'phase': phase})
            assert result['status'] == 'succeeded' and result['observed_revision_id'] == request['revision_id']
        return result
    submit(initial)
    observe(target, app, initial['variables'][0]['value'], initial['variables'][1]['value'])
    record('initial_real_delivery_and_pod_env', revision=initial['revision_id'])
    # Replaying the identical create-only revision must read back the same value.
    delivery.deliver(config, {**initial, 'phase': 'prepare'})
    conflict = copy.deepcopy(initial); conflict['phase'] = 'prepare'; conflict['variables'][1]['value'] += '-conflict'
    try:
        delivery.deliver(config, conflict)
    except delivery.DeliveryError as exc:
        assert exc.code == 'REVISION_CONFLICT', exc.code
    else:
        raise AssertionError('immutable revision overwrite was accepted')
    record('immutable_revision_replay_and_conflict_rejection')
    old = delivery.private_json(Path(target['vault']['token_file']))
    with delivery.Executor(target, app) as executor:
        rotated = delivery.rotate_delivery_credential(target, executor, 'live-rotation-01', retire_previous=True)
        assert rotated['status'] == 'succeeded' and rotated['previous_retired']
        current = delivery.private_json(Path(target['vault']['token_file']))
        assert old != current and executor.login(current)
        try:
            executor.login(old)
        except delivery.DeliveryError as exc:
            assert exc.code == 'VAULT_OPERATION_REJECTED', exc.code
        else:
            raise AssertionError('retired credential still authenticates')
    record('new_approle_login_custody_handoff_old_explicit_revocation_and_denial')
    changed = copy.deepcopy(initial); changed.update(operation_id='canary-delivery-02', revision_id='canary-revision-02')
    changed['variables'][0]['value'] += '-changed'; changed['variables'][1]['value'] += '-changed'
    submit(changed); observe(target, app, changed['variables'][0]['value'], changed['variables'][1]['value'])
    record('changed_plain_and_secret_revision_after_rotation', revision=changed['revision_id'])
    deleted = copy.deepcopy(changed); deleted.update(operation_id='canary-delivery-03', revision_id='canary-revision-03')
    deleted['variables'] = deleted['variables'][:1]
    submit(deleted); observe(target, app, deleted['variables'][0]['value'], None)
    record('deleted_secret_absent_from_new_pod', revision=deleted['revision_id'])
    # Historical immutable data is deliberately retained; deletion means unbinding.
    delivery.atomic_private(Path(output).with_name('retained-secret-request.json'), changed)
    record('historical_revision_retained_for_restart_observation', revision=changed['revision_id'])
    with delivery.Executor(target, app) as executor:
        path = 'railshot/data/projects/' + changed['project_id'] + '/revisions/' + changed['revision_id']
        expected = {v['name']: v['value'] for v in changed['variables'] if v['kind'] == 'secret'}
        assert executor.vault(['read', '-format=json', path], json_output=True)['data']['data'] == expected
        old_pod = executor.kube('-n', 'railshot-secrets', 'get', 'pod', 'vault-0', '-o', 'json')
        assert old_pod['metadata']['labels']['railshot.io/environment'] == initial['environment_id']
        remote = shlex.join(['sudo', '-n', 'k3s', 'kubectl', '-n', 'railshot-secrets',
                             'delete', 'pod', 'vault-0', '--wait=true', '--timeout=180s'])
        result = subprocess.run([*executor.prefix, remote], capture_output=True, text=True, timeout=210)
        if result.returncode: raise RuntimeError('approved canary Vault restart failed')
        deadline = time.monotonic() + 240
        while True:
            try:
                pod = executor.kube('-n', 'railshot-secrets', 'get', 'pod', 'vault-0', '-o', 'json')
                observed = executor.vault(['read', '-format=json', path], json_output=True)['data']['data']
                if pod['metadata']['uid'] != old_pod['metadata']['uid'] and observed == expected:
                    break
            except delivery.DeliveryError:
                pass
            if time.monotonic() >= deadline: raise RuntimeError('Vault did not preserve the existing revision after restart')
            time.sleep(2)
    # No put/reapply is issued after restart: the authenticated read above must
    # recover the already persisted value, not reconstruct it from the fixture.
    observe(target, app, deleted['variables'][0]['value'], None)
    record('vault_pod_replacement_auto_unseal_authenticated_persisted_value_read_and_deleted_env_still_absent')
    return {'status': 'succeeded', 'checks': len(checks)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True); parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.config, args.request, args.output)))
    except delivery.DeliveryError as exc:
        print(json.dumps({'status': 'failed', 'code': exc.code})); raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'type': type(exc).__name__})); raise SystemExit(1)
