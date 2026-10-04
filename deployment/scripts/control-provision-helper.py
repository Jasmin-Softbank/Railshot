#!/usr/bin/env python3
"""Restricted sudo boundary: one environment ID, root-owned fixed configuration."""
import json
import fcntl
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import subprocess
import sys

CONFIG = Path('/etc/railshot/control-provisioner.json')
SCRIPT = Path('/usr/local/libexec/railshot/control-environment-provision.sh')
ENVIRONMENT = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')


def owned(path, *, directory=False):
    path = Path(path)
    for ancestor in path.parents:
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe privileged ancestor')
    info = path.lstat()
    if ((not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
            or info.st_uid != 0 or info.st_mode & (0o077 if directory else 0o022)):
        raise ValueError('unsafe privileged reference')
    return info


def provision(environment, *, execute=subprocess.run):
    if os.geteuid() != 0 or not isinstance(environment, str) or ENVIRONMENT.fullmatch(environment) is None:
        raise ValueError('restricted privileged helper')
    info = owned(CONFIG)
    if info.st_mode & 0o077: raise ValueError('private configuration required')
    config = json.loads(CONFIG.read_text())
    if (not isinstance(config, dict) or set(config) != {'version', 'caller_uid', 'caller_gid', 'output_root', 'root_state_dir', 'provision_config'}
            or config['version'] != 1 or type(config['caller_uid']) is not int or config['caller_uid'] <= 0
            or type(config['caller_gid']) is not int or config['caller_gid'] < 0):
        raise ValueError('invalid helper configuration')
    caller = os.environ.get('SUDO_UID')
    if caller is None or caller != str(config['caller_uid']):
        raise ValueError('unregistered helper caller')
    root = Path(config['root_state_dir']); output_root = Path(config['output_root'])
    configuration = Path(config['provision_config'])
    if any(not path.is_absolute() for path in (root, output_root, configuration)):
        raise ValueError('absolute references required')
    owned(root, directory=True); owned(configuration); owned(SCRIPT)
    for name in ('control_vault.py', 'recovery_enroll.py'):
        owned(SCRIPT.parent / name)
    parent_info = output_root.lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != config['caller_uid'] or parent_info.st_mode & 0o077:
        raise ValueError('private registered consumer directory required')
    source = root / environment; output = output_root / environment
    lockfd = os.open(root / ('.' + environment + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    completed = execute(['bash', str(SCRIPT), '--config', str(configuration), '--environment-id', environment,
                         '--output-dir', str(source)], capture_output=True, text=True, timeout=180,
                        env={'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'})
    if completed.returncode: raise ValueError('provisioning did not complete')
    receipt = json.loads(completed.stdout)
    if receipt.get('environment_id') != environment or Path(receipt.get('profile_path', '')) != source / 'transit-profile.json':
        raise ValueError('issuance receipt differs')
    entries = list(source.rglob('*'))
    if len(entries) > 100: raise ValueError('unexpected issuance artifacts')
    expected = {}
    for path in entries:
        info = path.lstat()
        if info.st_uid != 0 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError('unsafe issuance artifact')
        if stat.S_ISREG(info.st_mode):
            raw = path.read_bytes()
            # Generated JSON references move with the environment copy; root key paths never do.
            if path.suffix == '.json': raw = raw.replace(str(source).encode(), str(output).encode())
            expected[path.relative_to(source)] = raw
    # All central material is now in memory. Never inspect or write a consumer-controlled tree as root.
    os.setgroups([]); os.setgid(config['caller_gid']); os.setuid(config['caller_uid'])
    if os.geteuid() != config['caller_uid']: raise ValueError('privilege drop failed')
    replace = True
    if output.exists() or output.is_symlink():
        info = output.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != config['caller_uid'] or info.st_mode & 0o077:
            raise ValueError('unsafe consumer output')
        replace = False
        for relative, raw in expected.items():
            destination = output / relative
            try:
                info = destination.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != config['caller_uid'] or info.st_mode & 0o077 or destination.read_bytes() != raw:
                    replace = True; break
            except FileNotFoundError:
                replace = True; break
    if replace:
        temporary = Path(tempfile.mkdtemp(prefix='.provision-', dir=output_root))
        for relative, raw in expected.items():
            destination = temporary / relative; destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as handle: handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        retained = None
        if output.exists():
            retained = output_root / (environment + '.retained-' + uuid.uuid4().hex)
            os.rename(output, retained)
        try:
            os.rename(temporary, output)
        except OSError:
            if retained is not None and not output.exists(): os.rename(retained, output)
            raise
    os.close(lockfd)
    return {**receipt, 'profile_path': str(output / 'transit-profile.json')}


def main():
    try:
        if len(sys.argv) != 2: raise ValueError('one environment ID required')
        print(json.dumps(provision(sys.argv[1]), separators=(',', ':')))
        return 0
    except Exception:
        print('{"status":"blocked","error":"CENTRAL_PROVISIONING_UNAVAILABLE"}')
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
