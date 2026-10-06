#!/usr/bin/env python3
"""Operator-only Codex account delivery. No plaintext credentials traverse SSM/GitHub."""
import argparse
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

ACCOUNT = '721622471953'
REGION = 'ap-northeast-2'
INSTANCE = 'i-09955d23ad1d8dbe2'


def require(value, code):
    if not value:
        raise ValueError(code)


def private_auth(path):
    path = Path(path).absolute()
    info = path.lstat()
    require(path.resolve() == path and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
            'PRIVATE_AUTH_FILE_REQUIRED')
    require(info.st_size <= 12288, 'AUTH_FILE_TOO_LARGE')
    auth = json.loads(path.read_bytes())
    tokens = auth.get('tokens', {})
    require(auth.get('auth_mode') == 'chatgpt' and not auth.get('OPENAI_API_KEY')
            and all(isinstance(tokens.get(k), str) and tokens[k]
                    for k in ('account_id', 'access_token', 'refresh_token')), 'SUBSCRIPTION_AUTH_REQUIRED')
    return auth


def receiver(action, nonce, encrypted=None):
    # This function is sent as trusted code to the fixed build host; only CMS ciphertext is input.
    import base64, contextlib, fcntl, hashlib, json, os, pathlib, re, shutil, stat, subprocess, time
    def check(ok, code):
        if not ok:
            raise ValueError(code)
    def private(path, directory=False):
        info = path.lstat()
        check(path.resolve() == path and info.st_uid == 0 and not info.st_mode & 0o077
              and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)), 'PRIVATE_HOST_PATH_REQUIRED')
    check(os.geteuid() == 0 and re.fullmatch(r'[a-f0-9]{32}', nonce), 'OPERATOR_HOST_REQUIRED')
    root = pathlib.Path('/var/lib/railshot-runner/credential-rotation')
    root.mkdir(mode=0o700, exist_ok=True); private(root, True)
    stage = root / nonce
    if action == 'prepare':
        stage.mkdir(mode=0o700)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(stage / 'key.pem'), '-out', str(stage / 'cert.pem'),
                        '-subj', '/CN=railshot-platform-credential', '-days', '1'], check=True, capture_output=True)
        return {'certificate': (stage / 'cert.pem').read_text()}
    private(stage, True)
    if action == 'discard':
        shutil.rmtree(stage)
        return {'status': 'discarded'}
    check(action == 'apply' and time.time() - stage.stat().st_mtime < 600, 'DELIVERY_EXPIRED')
    try:
        (stage / 'envelope.cms').write_bytes(base64.b64decode(encrypted, validate=True))
        subprocess.run(['openssl', 'cms', '-decrypt', '-binary', '-inform', 'DER',
                        '-in', str(stage / 'envelope.cms'), '-recip', str(stage / 'cert.pem'),
                        '-inkey', str(stage / 'key.pem'), '-out', str(stage / 'envelope.json')], check=True, capture_output=True)
        document = json.loads((stage / 'envelope.json').read_text())
        auth, model = document['auth'], document['model']
        check(auth.get('auth_mode') == 'chatgpt' and not auth.get('OPENAI_API_KEY')
              and all(isinstance(auth.get('tokens', {}).get(k), str) and auth['tokens'][k]
                      for k in ('account_id', 'access_token', 'refresh_token')), 'SUBSCRIPTION_AUTH_REQUIRED')
        check(isinstance(model, str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,79}', model), 'MODEL_INVALID')
        fingerprint = hashlib.sha256(auth['tokens']['account_id'].encode()).hexdigest()
        check(fingerprint == document['account_sha256'], 'ACCOUNT_BINDING_MISMATCH')
        home = pathlib.Path('/var/lib/railshot-runner/codex'); private(home, True)
        destination = home / 'auth.json'; private(destination)
        binding = home / 'railshot-account.json'
        if binding.exists() or binding.is_symlink():
            private(binding)
        locks = pathlib.Path('/var/lib/railshot-runner/work/.capacity')
        locks.mkdir(mode=0o700, parents=True, exist_ok=True); private(locks, True)
        with contextlib.ExitStack() as stack:
            # Existing agent capacity uses up to 16 slots. Hold every slot across the two-file replacement.
            for index in range(16):
                fd = os.open(locks / f'agent-{index}.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                check(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600,
                      'PRIVATE_CAPACITY_LOCK_REQUIRED')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('AGENT_BUSY') from None
            backup = root / ('previous-' + nonce); backup.mkdir(mode=0o700)
            for path in (destination, binding):
                if path.exists():
                    shutil.copyfile(path, backup / path.name); (backup / path.name).chmod(0o600)
            metadata = {'version': 1, 'owner': 'platform', 'account_sha256': fingerprint,
                        'model': model, 'rotation_id': nonce, 'rotated_at': int(time.time())}
            try:
                for path, value in ((destination, auth), (binding, metadata)):
                    pending = home / ('.' + path.name + '.' + nonce)
                    with pending.open('x') as stream:
                        pending.chmod(0o600); json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
                    os.replace(pending, path)
                check(json.loads(destination.read_text()) == auth and json.loads(binding.read_text()) == metadata,
                      'READBACK_FAILED')
                directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except BaseException:
                for path in (destination, binding):
                    saved = backup / path.name
                    if saved.exists():
                        os.replace(saved, path)
                    else:
                        path.unlink(missing_ok=True)
                raise
            return {'status': 'installed', **metadata, 'auth_mode': 'subscription', 'file_mode': '0600'}
    finally:
        shutil.rmtree(stage)


def aws(*arguments):
    result = subprocess.run(['aws', *arguments, '--region', REGION, '--output', 'json', '--no-cli-pager'],
                            capture_output=True, text=True, timeout=60)
    require(result.returncode == 0, 'AWS_REQUEST_FAILED')
    return json.loads(result.stdout)


def deliver(action, nonce, encrypted=None):
    source = inspect.getsource(receiver) + '\nimport json\ntry:\n print(json.dumps(receiver(' + repr(action) + ', ' + repr(nonce) + ', ' + repr(encrypted) + ')))\nexcept Exception as exc:\n print(json.dumps({"status":"blocked","code":str(exc) if isinstance(exc, ValueError) else "HOST_DELIVERY_FAILED"}))\n'
    command = "python3 - <<'RAILSHOT_CREDENTIAL'\n" + source + '\nRAILSHOT_CREDENTIAL'
    try:
        sent = aws('ssm', 'send-command', '--instance-ids', INSTANCE, '--document-name', 'AWS-RunShellScript',
                   '--comment', 'railshot-credential-' + nonce + '-' + action,
                   '--parameters', json.dumps({'commands': [command]}))
    except Exception:
        raise ValueError('DELIVERY_SUBMISSION_UNKNOWN:' + nonce) from None
    identity = sent['Command']['CommandId']
    for _ in range(30):
        time.sleep(2)
        try:
            result = aws('ssm', 'get-command-invocation', '--command-id', identity, '--instance-id', INSTANCE)
            if result['Status'] in ('Pending', 'InProgress', 'Delayed'):
                continue
            require(result['Status'] == 'Success', 'SSM_DELIVERY_FAILED')
            receipt = json.loads(result['StandardOutputContent'])
            require(isinstance(receipt, dict) and (action != 'apply' or receipt.get('status') in ('installed', 'blocked')),
                    'INVALID_DELIVERY_RECEIPT')
        except Exception:
            raise ValueError('DELIVERY_OUTCOME_UNKNOWN:' + identity) from None
        require(receipt.get('status') != 'blocked', receipt.get('code', 'HOST_DELIVERY_BLOCKED'))
        return receipt
    # Never retry an unobserved apply automatically. Query this command before another rotation.
    raise ValueError('DELIVERY_OUTCOME_UNKNOWN:' + identity)


def rotate(auth_file, model):
    require(not os.environ.get('GITHUB_ACTIONS'), 'OPERATOR_MACHINE_REQUIRED')
    require(re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,79}', model), 'MODEL_INVALID')
    auth = private_auth(auth_file)
    require(aws('sts', 'get-caller-identity')['Account'] == ACCOUNT, 'AWS_ACCOUNT_MISMATCH')
    instance = aws('ec2', 'describe-instances', '--instance-ids', INSTANCE)['Reservations'][0]['Instances'][0]
    require(instance['State']['Name'] == 'running' and any(t['Key'] == 'Name' and t['Value'] == 'railshot-build-worker-aws-01'
            for t in instance.get('Tags', [])), 'BUILD_HOST_MISMATCH')
    nonce = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='railshot-platform-credential-') as temporary:
        root = Path(temporary).resolve(); candidate = root / 'auth.json'
        candidate.write_text(json.dumps(auth)); candidate.chmod(0o600)
        # Dedicated auth-only home: never read user MCP/config/project files during the compatibility check.
        probe = subprocess.run(['codex', 'exec', '--json', '--model', model, '--sandbox', 'read-only',
                                '--skip-git-repo-check', '-C', temporary, 'Reply only OK. Do not call tools.'],
                               env={**os.environ, 'CODEX_HOME': temporary, 'OPENAI_API_KEY': '', 'CODEX_API_KEY': ''},
                               capture_output=True, text=True, timeout=90)
        require(probe.returncode == 0, 'ACCOUNT_MODEL_PREFLIGHT_FAILED')
        require(any(json.loads(line).get('type') == 'turn.completed' for line in probe.stdout.splitlines() if line.startswith('{')),
                'ACCOUNT_MODEL_PREFLIGHT_FAILED')
        auth = private_auth(candidate)  # Includes any provider token refresh from the preflight.
        fingerprint = hashlib.sha256(auth['tokens']['account_id'].encode()).hexdigest()
        (root / 'envelope.json').write_text(json.dumps({'auth': auth, 'model': model, 'account_sha256': fingerprint}))
        (root / 'envelope.json').chmod(0o600)
        certificate = deliver('prepare', nonce)['certificate']
        (root / 'cert.pem').write_text(certificate)
        try:
            subprocess.run(['openssl', 'cms', '-encrypt', '-binary', '-aes-256-cbc', '-in', str(root / 'envelope.json'),
                            '-outform', 'DER', '-out', str(root / 'envelope.cms'), str(root / 'cert.pem')],
                           check=True, capture_output=True, timeout=15)
        except Exception:
            deliver('discard', nonce)
            raise
        return deliver('apply', nonce, base64.b64encode((root / 'envelope.cms').read_bytes()).decode())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--auth-file', required=True, help='Private platform-owned Codex auth.json; never a secret value')
    parser.add_argument('--model', required=True, help='Model to validate with this platform account')
    args = parser.parse_args()
    try:
        print(json.dumps(rotate(args.auth_file, args.model)))
    except Exception as exc:
        # Do not expose subprocess output, JSON parser input, provider messages, or tokens.
        code = str(exc) if type(exc) is ValueError and re.fullmatch(r'[A-Z_:0-9a-f-]+', str(exc)) else 'CREDENTIAL_ROTATION_FAILED'
        print(json.dumps({'status': 'blocked', 'code': code}))
        raise SystemExit(1)
