#!/usr/bin/env python3
"""Standalone removal helper: only fixed, installation-owned local resources."""
import json
import os
from pathlib import Path
import shutil
import pwd
import stat
import subprocess
import sys
import urllib.parse
import urllib.request

MARKER = '# Managed by RailShot personal client v1\n'
PAYLOAD = Path('/opt/railshot/personal')
CONFIG = Path('/etc/railshot-personal')
STATE = Path('/var/lib/railshot-personal')
CLI_HOME = Path('/var/lib/railshot-personal-cli')
CLI_USER = 'railshot-openstack'
UNIT = Path('/etc/systemd/system/railshot-personal.service')
RUNTIME_UNIT = Path('/etc/systemd/system/railshot-personal-runtime.service')
WG = Path('/etc/wireguard/railshot0.conf')


def require(value):
    if not value:
        raise ValueError('REMOVAL_OWNERSHIP_UNVERIFIED')


def safe(path, directory=False, owner=None):
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    info = path.stat()
    require(info.st_uid == (os.geteuid() if owner is None else owner) and not info.st_mode & 0o022)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45, check=False)
    require(result.returncode == 0)
    return result.stdout.strip()


def inspect(config):
    # Validate every deletion target before making any change. No paths are taken
    # from the remote command; namespaces, VMs and provider resources are excluded.
    for path in (UNIT, RUNTIME_UNIT, WG):
        if path.exists():
            safe(path)
            require(path.read_text().startswith(MARKER))
    if PAYLOAD.exists():
        safe(PAYLOAD, True)
        safe(PAYLOAD / '.railshot-personal.json')
        require(json.loads((PAYLOAD / '.railshot-personal.json').read_text()).get('version') == 1)
    if CONFIG.exists():
        safe(CONFIG, True)
        safe(CONFIG / 'client.json')
        stored = json.loads((CONFIG / 'client.json').read_text())
        require(all(stored.get(k) == config.get(k) for k in ('target_id', 'generation', 'client_token')))
        require(set(p.name for p in CONFIG.iterdir()) <= {'client.json', 'credentials.key', 'credentials.enc', 'wireguard.key', 'runtime_host_key', 'runtime_host_key.pub', 'runtime_authorized_keys', 'runtime_sshd_config', 'cli-account.json'})
    marker = CONFIG / 'cli-account.json'
    if marker.exists():
        safe(marker)
        owned = json.loads(marker.read_text())
        account = pwd.getpwnam(CLI_USER)
        require(owned == {'user': CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}
                and account.pw_uid != 0 and account.pw_dir == str(CLI_HOME))
        if CLI_HOME.exists():
            safe(CLI_HOME, True, account.pw_uid)
            require(all(p.name in ('credentials.key', 'credentials.enc', 'control.json', 'authorized_keys', 'resources.json', 'jobs.lock')
                        or p.name.startswith('job-') and p.suffix == '.json' for p in CLI_HOME.iterdir()))
    else:
        require(not CLI_HOME.exists())
    if STATE.exists():
        safe(STATE, True)
        require(all(p.name == 'daemon.lock' or p.name.startswith('job-') and p.suffix == '.json' for p in STATE.iterdir()))


def cleanup(config, runner=run):
    inspect(config)
    steps = []
    runner(['systemctl', 'disable', '--now', 'railshot-personal'])
    require(runner(['systemctl', 'show', 'railshot-personal', '--property=ActiveState', '--value']) in ('inactive', 'failed'))
    steps.append({'name': 'client-service', 'status': 'succeeded'})
    if RUNTIME_UNIT.exists():
        runner(['systemctl', 'disable', '--now', 'railshot-personal-runtime'])
        require(runner(['systemctl', 'show', 'railshot-personal-runtime', '--property=ActiveState', '--value']) in ('inactive', 'failed'))
    runner(['systemctl', 'disable', '--now', 'wg-quick@railshot0'])
    require('railshot0' not in runner(['wg', 'show', 'interfaces']).split())
    for path in (UNIT, RUNTIME_UNIT, WG):
        if path.exists():
            path.unlink()
    runner(['systemctl', 'daemon-reload'])
    steps.append({'name': 'client-tunnel', 'status': 'succeeded'})
    if (CONFIG / 'cli-account.json').exists():
        runner(['userdel', CLI_USER])
        if CLI_HOME.exists():
            shutil.rmtree(CLI_HOME)
        require(not CLI_HOME.exists())
    for path in (PAYLOAD, CONFIG, STATE):
        if path.exists():
            shutil.rmtree(path)
        require(not path.exists() and not path.is_symlink())
    require(not UNIT.exists() and not RUNTIME_UNIT.exists() and not WG.exists())
    steps.append({'name': 'client-files', 'status': 'succeeded'})
    return steps


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('HTTP_REDIRECT_REJECTED')


def send(config, result):
    base = config['api_url']
    parsed = urllib.parse.urlsplit(base)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment)
    request = urllib.request.Request(base.rstrip('/') + '/api/v1/targets/' + config['target_id'] + '/receipts',
                                    data=json.dumps(result).encode(), method='POST', headers={
                                    'Authorization': 'Bearer ' + config['client_token'], 'Content-Type': 'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
        require(200 <= response.status < 300)


def main():
    config_path = Path(sys.argv[1])
    self_path = Path(__file__)
    safe(config_path)
    require(config_path.stat().st_mode & 0o077 == 0)
    config = json.loads(config_path.read_text())
    result = {'operation_id': config['operation_id'], 'generation': config['generation'],
              'status': 'unknown', 'steps': [], 'residuals': ['client_removal_unverified'], 'client_removed': False}
    try:
        result['steps'] = cleanup(config)
        # Remove temporary secret and executable before attesting completion.
        config_path.unlink()
        self_path.unlink()
        require(not config_path.exists() and not self_path.exists())
        result.update(status='succeeded', residuals=[], client_removed=True)
    except Exception:
        # No receipt of success is emitted for partial cleanup.
        for path in (config_path, self_path):
            if path.exists():
                path.unlink()
    try:
        send(config, result)
    except Exception:
        # Server remains unknown when the last receipt is lost. No local success
        # log is substituted for server acknowledgement.
        print('RailShot removal receipt could not be delivered.', file=sys.stderr)
        return 1
    return 0 if result['client_removed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
