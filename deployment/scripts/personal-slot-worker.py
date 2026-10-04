#!/usr/bin/env python3
"""Fixed, root-only SSH entry point for the dedicated personal runtime route slot.

Install outside the legacy worker tree. Its sudoers rule must allow this program
with no arguments only; the dedicated SSH key must force that command and disable
forwarding/PTY. The existing route worker protocol remains unchanged. Mounts live
only in the child mount namespace and disappear when the request finishes.
"""
import os
from pathlib import Path
import stat
import subprocess
import sys

ROOT = Path('/opt/railshot/personal-slot/slot-20261004')
BINDINGS = (
    (ROOT / 'worker', Path('/opt/railshot/octavia')),
    (ROOT / 'state', Path('/var/lib/railshot/octavia')),
    (ROOT / 'certificates', Path('/var/lib/octavia/certificates')),
)
ENVIRONMENT = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}


def private_directory(path):
    if path.resolve() != path:
        raise ValueError('unsafe_slot_path')
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe_slot_directory')
    if path.stat().st_mode & 0o077:
        raise ValueError('slot_directory_not_private')


def private_file(path):
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError('unsafe_slot_file')


def validate():
    private_directory(ROOT)
    for source, target in BINDINGS:
        private_directory(source)
        # Existing targets may be service-readable but must not be writable by
        # another account, symlinks, or newly invented mount destinations.
        info = target.lstat()
        if target.resolve() != target or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise ValueError('unsafe_mount_target')
    for name in ('openstack_route_worker.py', 'route-config.json', 'cloud-cli.py', 'inputs.json'):
        private_file(ROOT / 'worker' / name)
    private_directory(ROOT / 'worker' / 'origin-ca')
    private_file(ROOT / 'worker' / 'origin-ca' / 'ca.pem')


def execute(arguments, runner=subprocess.run):
    if os.geteuid() != 0:
        raise ValueError('root_required')
    validate()
    if not arguments:
        inode = os.stat('/proc/self/ns/mnt').st_ino
        command = ['/usr/bin/unshare', '--mount', '--propagation', 'private',
                   '/usr/bin/python3', '-I', str(Path(__file__).resolve()), '--inside', str(inode)]
        return runner(command, env=ENVIRONMENT, check=False).returncode
    if len(arguments) != 2 or arguments[0] != '--inside' or not arguments[1].isdigit():
        raise ValueError('invalid_arguments')
    if os.stat('/proc/self/ns/mnt').st_ino == int(arguments[1]):
        raise ValueError('mount_namespace_not_isolated')
    for source, target in BINDINGS:
        runner(['/usr/bin/mount', '--bind', str(source), str(target)],
               env=ENVIRONMENT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return runner(['/usr/bin/python3', '-I', '/opt/railshot/octavia/openstack_route_worker.py'],
                  env=ENVIRONMENT, check=False).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(execute(sys.argv[1:]))
    except (OSError, ValueError, subprocess.SubprocessError):
        # Neither a provider exception nor an SSH environment value may leak.
        print('{"status":"blocked","reason":"personal_slot_unavailable"}')
        raise SystemExit(1)
