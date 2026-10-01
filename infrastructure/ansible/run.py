#!/usr/bin/env python3
"""Trusted local adapter. JSON only; no arbitrary playbooks, variables or shell."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCHEMA = ROOT / 'contracts/ansible-request.schema.json'
PLAYBOOKS = {'guest': HERE / 'guest.yml', 'runtime': HERE / 'runtime.yml'}
PRIVATE = tuple(ipaddress.ip_network(x) for x in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
MAX_BYTES = 1024 * 1024


class ContractError(ValueError):
    pass


def check_schema(value, rule, path='$'):
    """Validate the deliberately small, checked-in JSON Schema subset."""
    kind = rule.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int}
    if kind and type(value) is not types[kind]:
        raise ContractError(f'{path}: invalid type')
    if 'const' in rule and value != rule['const']:
        raise ContractError(f'{path}: invalid constant')
    if 'enum' in rule and value not in rule['enum']:
        raise ContractError(f'{path}: unsupported value')
    if kind == 'object':
        props = rule.get('properties', {})
        if not set(rule.get('required', [])).issubset(value):
            raise ContractError(f'{path}: missing required field')
        if rule.get('additionalProperties') is False and set(value) - set(props):
            raise ContractError(f'{path}: unknown field')
        for key, child in value.items():
            if key in props:
                check_schema(child, props[key], f'{path}.{key}')
    if kind == 'array':
        if not rule.get('minItems', 0) <= len(value) <= rule.get('maxItems', MAX_BYTES):
            raise ContractError(f'{path}: invalid item count')
        for child in value:
            check_schema(child, rule['items'], f'{path}[]')
    if kind == 'string':
        if not rule.get('minLength', 0) <= len(value) <= rule.get('maxLength', MAX_BYTES):
            raise ContractError(f'{path}: invalid length')
        if 'pattern' in rule and re.fullmatch(rule['pattern'], value) is None:
            raise ContractError(f'{path}: invalid format')
    if kind == 'integer' and not rule.get('minimum', -MAX_BYTES) <= value <= rule.get('maximum', MAX_BYTES):
        raise ContractError(f'{path}: outside allowed range')
    # The schema's single conditional separates Patroni from guest/runtime inputs.
    for conditional in rule.get('allOf', []):
        expected = conditional['if']['properties']['operation']['const']
        branch = conditional['then'] if value.get('operation') == expected else conditional['else']
        if not set(branch.get('required', [])).issubset(value):
            raise ContractError(f'{path}: missing operation input')
        if 'not' in branch and set(branch['not']['required']).issubset(value):
            raise ContractError(f'{path}: unexpected operation input')


def validate(request):
    check_schema(request, json.loads(SCHEMA.read_text()))
    inventory = request.get('inventory', {})
    nodes = inventory.get('control_plane', []) + inventory.get('workers', [])
    ids, addresses, resources = set(), set(), set()
    for node in nodes:
        try:
            addr = ipaddress.IPv4Address(node['private_ipv4'])
        except ipaddress.AddressValueError as exc:
            raise ContractError('inventory: invalid IPv4 address') from exc
        if not any(addr in network for network in PRIVATE):
            raise ContractError('inventory: RFC1918 private IPv4 required')
        if node['id'] in ('all', 'ungrouped') or node['id'] in ids or str(addr) in addresses or node['resource_id'] in resources:
            raise ContractError('inventory: duplicate node identity, resource or address')
        ids.add(node['id']); addresses.add(str(addr)); resources.add(node['resource_id'])
        for field in ('identity_file', 'known_hosts_file'):
            p = Path(node['ssh'][field])
            if '..' in p.parts:
                raise ContractError('ssh: parent traversal is not allowed')
    placements = request.get('patroni', {}).get('placements', [])
    seen = set()
    for placement in placements:
        key = (placement['provider'], placement['site'])
        if key in seen or placement['database_nodes'] + placement['dcs_voters'] == 0:
            raise ContractError('patroni: duplicate or empty placement')
        seen.add(key)
    if placements and not sum(p['database_nodes'] for p in placements):
        raise ContractError('patroni: at least one database node must be requested')
    return request


def base_result(request=None):
    request = request or {}
    return {'schema_version': '1.0', 'request_id': request.get('request_id'),
            'target_id': request.get('target', {}).get('id'), 'operation': request.get('operation'),
            'status': 'failed', 'stage': 'validation', 'guest_ready': False,
            'runtime_ready': False, 'application_ready': False, 'public_http_verified': False,
            'steps': [], 'error': None}


def fail(result, status, code, message, *, unknown=False):
    result.update(status=status, error={'code': code, 'message': message,
                                       'retryable': False, 'outcome_unknown': unknown})
    return result


def support_error(request):
    if request['operation'] == 'patroni.install':
        return ('PATRONI_PLAYBOOK_UNAVAILABLE', 'Placement is recorded; no team Patroni playbook is supplied.')
    inv = request['inventory']
    if len(inv['control_plane']) != 1 or inv['workers']:
        return ('SINGLE_NODE_ONLY', 'Current runtime supports one control-plane node and no workers.')
    if request['operation'] == 'runtime.install' and request['target']['architecture'] != 'amd64':
        return ('RUNTIME_ARCHITECTURE_UNVERIFIED', 'The first integrated runtime profile is amd64; ARM64 remains unverified.')
    return None


def private_file(path, *, identity=False):
    p = Path(path)
    try:
        info = p.lstat()
    except OSError as exc:
        raise ContractError('SSH identity/known-hosts reference is unavailable') from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size == 0:
        raise ContractError('SSH references must be nonempty regular files owned by the executor')
    if info.st_mode & (0o077 if identity else 0o022):
        raise ContractError('SSH reference file permissions are too broad')


def build_inventory(request):
    node = request['inventory']['control_plane'][0]
    ssh = node['ssh']
    # Paths only allow simple absolute path characters. No ProxyCommand/config inheritance.
    common = ('-F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none '
              '-o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null '
              '-o ProxyCommand=none -o ProxyJump=none -o ForwardAgent=no '
              '-o ClearAllForwardings=yes -o PasswordAuthentication=no '
              '-o KbdInteractiveAuthentication=no -o GSSAPIAuthentication=no '
              f'-o UserKnownHostsFile={ssh["known_hosts_file"]}')
    host = {'ansible_host': node['private_ipv4'], 'ansible_user': ssh['user'],
            'ansible_port': ssh['port'], 'ansible_connection': 'ssh',
            'ansible_ssh_private_key_file': ssh['identity_file'],
            'ansible_ssh_common_args': common,
            'ansible_become_method': 'sudo',
            'ansible_become_flags': '-H -S -n'}
    return {'all': {'children': {'k3s_server': {'hosts': {node['id']: host}},
                                 'k3s_workers': {'hosts': {}}}}}


def child_env():
    # Do not inherit arbitrary Ansible plugins, extra config, SSH agent or cloud credentials.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
    env.update(LANG='en_US.UTF-8' if sys.platform == 'darwin' else 'C.UTF-8',
               LC_ALL='en_US.UTF-8' if sys.platform == 'darwin' else 'C.UTF-8',
               ANSIBLE_CONFIG=str(HERE / 'ansible.cfg'), ANSIBLE_HOST_KEY_CHECKING='True',
               ANSIBLE_RETRY_FILES_ENABLED='False', ANSIBLE_NOCOLOR='True',
               ANSIBLE_SSH_ARGS='-o ControlMaster=no', ANSIBLE_BECOME_ASK_PASS='False')
    return env


def execute(argv, timeout, env):
    """Bound the complete local process group; never echo potentially sensitive output."""
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(argv, cwd=HERE, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise


def read_receipt(path, request, stage, nonce):
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ContractError('Playbook did not produce a valid readiness receipt') from exc
    expected = {'request_id': request['request_id'], 'target_id': request['target']['id'],
                'node_id': request['inventory']['control_plane'][0]['id'],
                'stage': stage, 'nonce': nonce}
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()) or receipt.get(stage + '_ready') is not True:
        raise ContractError('Readiness receipt does not match this request and stage')
    return receipt


def run(request, *, validate_only=False, runner=execute):
    try:
        validate(request)
    except (ContractError, OSError, ValueError) as exc:
        return fail(base_result(), 'invalid', 'INVALID_REQUEST', str(exc))
    result = base_result(request)
    blocked = support_error(request)
    if blocked:
        return fail(result, 'blocked', *blocked)
    if validate_only:
        result['status'] = 'validated'
        return result
    result['stage'] = 'executor_preflight'
    node = request['inventory']['control_plane'][0]
    try:
        private_file(node['ssh']['identity_file'], identity=True)
        private_file(node['ssh']['known_hosts_file'])
    except ContractError as exc:
        return fail(result, 'blocked', 'SSH_REFERENCE_UNAVAILABLE', str(exc))
    executable = shutil.which('ansible-playbook')
    if not executable:
        return fail(result, 'blocked', 'ANSIBLE_UNAVAILABLE', 'ansible-playbook is not installed on the executor')
    deadline = time.monotonic() + request['timeout_seconds']
    with tempfile.TemporaryDirectory(prefix='railshot-ansible-') as directory:
        work = Path(directory)
        inventory = work / 'inventory.json'
        inventory.write_text(json.dumps(build_inventory(request)))
        inventory.chmod(0o600)
        for stage in ('guest', 'runtime') if request['operation'] == 'runtime.install' else ('guest',):
            result['stage'] = stage
            nonce = secrets.token_hex(16)
            receipt_path = work / f'{stage}-receipt.json'
            extra = {'railshot_request_id': request['request_id'], 'railshot_target_id': request['target']['id'],
                     'railshot_nonce': nonce, 'railshot_receipt_path': str(receipt_path),
                     'railshot_resource_id': node['resource_id'],
                     'railshot_expected_arch': request['target']['architecture'],
                     'railshot_initialization': request['target']['initialization'],
                     'railshot_node_ip': node['private_ipv4'], 'k3s_api_host': node['private_ipv4'],
                     'k3s_node_name': node['id'], 'k3s_version': 'v1.34.11+k3s1'}
            extra_path = work / f'{stage}-vars.json'
            extra_path.write_text(json.dumps(extra)); extra_path.chmod(0o600)
            argv = [executable, '-i', str(inventory), str(PLAYBOOKS[stage]), '--extra-vars', '@' + str(extra_path)]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return fail(result, 'failed', 'EXECUTION_TIMEOUT', 'Request deadline exceeded', unknown=stage == 'runtime')
            try:
                rc = runner(argv, remaining, child_env())
            except subprocess.TimeoutExpired:
                return fail(result, 'failed', 'EXECUTION_TIMEOUT', 'Playbook deadline exceeded; inspect target state', unknown=stage == 'runtime')
            except OSError:
                return fail(result, 'failed', 'EXECUTOR_FAILURE', 'Unable to start the trusted Ansible executable')
            result['steps'].append({'stage': stage, 'exit_code': rc})
            if rc:
                code = 'GUEST_CHECK_FAILED' if stage == 'guest' else 'RUNTIME_INSTALL_FAILED'
                return fail(result, 'failed', code, 'Playbook failed; readiness was not established', unknown=stage == 'runtime')
            try:
                read_receipt(receipt_path, request, stage, nonce)
            except ContractError as exc:
                return fail(result, 'failed', 'READINESS_UNPROVEN', str(exc), unknown=stage == 'runtime')
            result[stage + '_ready'] = True
    result['status'] = 'succeeded'
    return result


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('Duplicate JSON object key')
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', default='-', help='JSON file or - for stdin')
    parser.add_argument('--validate-only', action='store_true', help='No SSH or file-reference checks; never marks readiness')
    args = parser.parse_args(argv)
    try:
        if args.request == '-':
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with open(args.request, 'rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ContractError('Request exceeds 1 MiB')
        request = json.loads(raw, object_pairs_hook=unique_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ContractError('Non-finite JSON number')))
        result = run(request, validate_only=args.validate_only)
    except (OSError, ValueError, UnicodeError):
        result = fail(base_result(), 'invalid', 'INVALID_JSON', 'Unreadable, duplicate-key or invalid JSON request')
    print(json.dumps(result, separators=(',', ':')))
    return {'succeeded': 0, 'validated': 0, 'invalid': 2, 'blocked': 3, 'failed': 4}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
