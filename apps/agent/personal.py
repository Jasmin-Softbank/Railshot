#!/usr/bin/env python3
"""Outbound personal environment client. No remote shell or arbitrary command API."""
import argparse
import base64
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import pwd
import tempfile
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'deployment/bootstrap'))
from client_setup.state import atomic_private_write, private_directory, read_private
from client_setup.credentials import CredentialStore
from infrastructure.providers.openstack.cli import OpenStackCLI

VERSION = '1.0.0'
CONFIG = Path('/etc/railshot-personal')
STATE = Path('/var/lib/railshot-personal')
PAYLOAD = Path('/opt/railshot/personal')
UNIT = Path('/etc/systemd/system/railshot-personal.service')
RUNTIME_UNIT = Path('/etc/systemd/system/railshot-personal-runtime.service')
WG = Path('/etc/wireguard/railshot0.conf')
MARKER = '# Managed by RailShot personal client v1\n'
CAPABILITIES = ['openstack.execute', 'client.remove']
CLI_USER = 'railshot-openstack'
CLI_HOME = Path('/var/lib/railshot-personal-cli')
IDENT = re.compile(r'[A-Za-z0-9_-]{1,128}')


def require(condition, code='CLIENT_INVALID'):
    if not condition:
        raise ValueError(code)


def run(args, *, input=None, timeout=30):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout, check=False)
    require(result.returncode == 0, 'CLIENT_COMMAND_FAILED')
    return result.stdout.strip()


def https_url(value):
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and not any(c.isspace() for c in value), 'HTTPS_REQUIRED')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('HTTP_REDIRECT_REJECTED')


def api(base, path, token, body):
    require(path.startswith('/api/v1/') and '\n' not in token and '\r' not in token)
    request = urllib.request.Request(https_url(base) + path, data=json.dumps(body).encode(),
                                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
    with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
        raw = response.read(1048577)
        require(len(raw) <= 1048576, 'RESPONSE_TOO_LARGE')
        result = json.loads(raw)
        require(isinstance(result, dict))
        if 'data' in result:
            result = result['data']
        require(isinstance(result, dict))
        return result


def wireguard_key(value):
    require(isinstance(value, str) and len(base64.b64decode(value, validate=True)) == 32)
    return value


def validate_tunnel(tunnel):
    wireguard_key(tunnel['server_public_key'])
    address = ipaddress.ip_interface(tunnel['address'])
    allowed = tunnel['allowed_ips']
    if isinstance(allowed, list):
        require(len(allowed) == 1)
        allowed = allowed[0]
    server = ipaddress.ip_network(allowed)
    require(address.version == server.version == 4 and address.network.prefixlen == server.prefixlen == 32
            and address.ip != server.network_address)
    require(re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}', tunnel['endpoint']))
    require(1 <= int(tunnel['endpoint'].rsplit(':', 1)[1]) <= 65535)
    return str(address), str(server)


def install_tunnel(config, runner=run):
    tunnel = config['tunnel']
    address, allowed = validate_tunnel(tunnel)
    secret = wireguard_key(read_private(CONFIG / 'wireguard.key').decode().strip())
    content = (MARKER + '[Interface]\nPrivateKey = ' + secret + '\nAddress = ' + address + '\n\n[Peer]\n'
               + 'PublicKey = ' + tunnel['server_public_key'] + '\nEndpoint = ' + tunnel['endpoint']
               + '\nAllowedIPs = ' + allowed + '\nPersistentKeepalive = 25\n')
    require(not any(p.is_symlink() for p in (WG, *WG.parents)))
    if WG.exists():
        require(WG.stat().st_uid == 0 and not WG.stat().st_mode & 0o077 and WG.read_text().startswith(MARKER), 'WIREGUARD_NOT_OWNED')
    WG.parent.mkdir(mode=0o700, exist_ok=True)
    # /etc/wireguard can be a root-owned shared directory; never change its mode.
    require(WG.parent.stat().st_uid == 0 and not WG.parent.stat().st_mode & 0o022)
    fd = os.open(str(WG) + '.pending', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(WG) + '.pending', WG)
    finally:
        if Path(str(WG) + '.pending').exists():
            Path(str(WG) + '.pending').unlink()
    runner(['systemctl', 'enable', '--now', 'wg-quick@railshot0'])


def checks(config, runner=run, cli_factory=OpenStackCLI):
    result = {'tunnel': False, 'openstack': False, 'runtime': False}
    try:
        rows = runner(['wg', 'show', 'railshot0', 'latest-handshakes']).splitlines()
        result['tunnel'] = any(len(parts) == 2 and parts[0] == config['tunnel']['server_public_key']
                               and 0 < int(parts[1]) <= time.time() and time.time() - int(parts[1]) <= 180
                               for parts in (row.split() for row in rows))
    except Exception:
        pass
    try:
        cli_factory(CredentialStore(CONFIG).load()).run(['server', 'list'])
        result['openstack'] = True
    except Exception:
        pass
    try:
        result['runtime'] = runner(['systemctl', 'is-active', 'railshot-personal-runtime']) == 'active'
    except Exception:
        pass
    return result


def save_config(config):
    atomic_private_write(CONFIG / 'client.json', json.dumps(config).encode())


def enroll(args):
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    private_directory(CONFIG)
    private_directory(STATE)
    existing = CONFIG / 'client.json'
    if existing.exists():
        config = json.loads(read_private(existing))
        require(config['api_url'] == https_url(args.api_url) and config['enrollment_id'] == args.enrollment_id, 'INSTALLATION_BINDING_CHANGED')
        install_tunnel(config)
        install_runtime_access(config)
        install_service()
        wait_control_ready(config)
        return
    require(IDENT.fullmatch(args.enrollment_id))
    vault = CredentialStore(CONFIG)
    if vault.path.exists():
        auth = vault.load()
    else:
        # Use existing project-scoped Application Credentials, never create admin
        # users, roles, networks, VMs, or provider credentials during enrollment.
        auth = {'auth_url': https_url(input('OpenStack 인증 URL (HTTPS): ').strip()),
                'application_credential_id': input('OpenStack Application Credential ID: ').strip(),
                'application_credential_secret': getpass.getpass('OpenStack Application Credential Secret: ')}
        require(auth['application_credential_id'] and auth['application_credential_secret'])
        OpenStackCLI(auth).run(['server', 'list'])
        vault.save(auth)
    project_id = args.project_id or input('OpenStack 프로젝트 ID: ').strip()
    require(IDENT.fullmatch(project_id))
    token_info = OpenStackCLI(auth).run(['token', 'issue'])
    require(token_info.get('project_id') == project_id, 'OPENSTACK_PROJECT_MISMATCH')
    key_path = CONFIG / 'wireguard.key'
    if not key_path.exists():
        atomic_private_write(key_path, (wireguard_key(run(['wg', 'genkey'])) + '\n').encode())
    public_key = wireguard_key(run(['wg', 'pubkey'], input=read_private(key_path).decode()))
    token = os.environ.pop('RAILSHOT_ENROLLMENT_TOKEN', '') or getpass.getpass('RailShot 일회용 등록 토큰: ')
    body = {'public_key': public_key, 'client_version': VERSION, 'project_id': project_id, 'capabilities': CAPABILITIES}
    host_key = CONFIG / 'runtime_host_key'
    if not host_key.exists():
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host_key)])
    require(host_key.stat().st_uid == 0 and not host_key.stat().st_mode & 0o077 and not host_key.is_symlink())
    body['runtime'] = {'ssh_host_key': ' '.join(host_key.with_suffix('.pub').read_text().split()[:2])}
    if args.profile_id:
        body['runtime']['profile_id'] = args.profile_id
    response = api(args.api_url, '/api/v1/enrollments/' + args.enrollment_id + '/claims', token, body)
    del token
    require(IDENT.fullmatch(response['target_id']) and type(response['generation']) is int and response['generation'] > 0)
    require(isinstance(response['client_token'], str) and response['client_token'])
    validate_tunnel(response['tunnel'])
    config = {k: response[k] for k in ('target_id', 'generation', 'client_token', 'tunnel')}
    if response.get('runtime_access'):
        config['runtime_access'] = response['runtime_access']
    config.update(api_url=https_url(args.api_url), enrollment_id=args.enrollment_id, project_id=project_id)
    # Save the issued credential before any OS configuration. A subsequent local
    # failure is resumable without replaying the consumed enrollment credential.
    save_config(config)
    install_tunnel(config)
    install_runtime_access(config)
    install_service()
    wait_control_ready(config)


def install_cli_account(config):
    marker = CONFIG / 'cli-account.json'
    try:
        account = pwd.getpwnam(CLI_USER)
    except KeyError:
        run(['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', str(CLI_HOME), '--shell', '/bin/sh', CLI_USER])
        # Disabled password authentication, but account is not marked locked for
        # OpenSSH public-key authentication with UsePAM=no.
        run(['usermod', '--password', '*', CLI_USER])
        account = pwd.getpwnam(CLI_USER)
        atomic_private_write(marker, json.dumps({'user': CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}).encode())
    require(marker.exists(), 'CLI_ACCOUNT_NOT_OWNED')
    owned = json.loads(read_private(marker))
    require(owned == {'user': CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}
            and account.pw_uid != 0 and account.pw_dir == str(CLI_HOME) and account.pw_shell == '/bin/sh', 'CLI_ACCOUNT_NOT_OWNED')
    control = {key: config[key] for key in ('project_id', 'target_id', 'generation')}
    if CLI_HOME.exists():
        require(not CLI_HOME.is_symlink() and CLI_HOME.stat().st_uid == account.pw_uid
                and not CLI_HOME.stat().st_mode & 0o077, 'CLI_HOME_UNSAFE')
        require(json.loads((CLI_HOME / 'control.json').read_text()) == control, 'CLI_BINDING_CHANGED')
        return
    staging = Path(tempfile.mkdtemp(prefix='.railshot-cli-', dir=CLI_HOME.parent))
    try:
        CredentialStore(staging).save(CredentialStore(CONFIG).load())
        atomic_private_write(staging / 'control.json', json.dumps(control).encode())
        for path in staging.iterdir():
            os.chown(path, account.pw_uid, account.pw_gid)
        os.chown(staging, account.pw_uid, account.pw_gid)
        staging.rename(CLI_HOME)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def install_runtime_access(config):
    access = config.get('runtime_access')
    if not access:
        return
    from apps.agent.install_forced_command import authorized_key_line
    address, allowed = validate_tunnel(config['tunnel'])
    install_cli_account(config)
    # Reuse strict key/source validation. SSH executes only the unprivileged
    # project-bound OpenStack adapter; no root shell or K3s access is installed.
    line = authorized_key_line(access['public_key'], allowed.split('/')[0],
                               str(PAYLOAD / '.venv/bin/python'), str(PAYLOAD), str(CONFIG))
    fixed = str(PAYLOAD / '.venv/bin/python') + ' -I ' + str(PAYLOAD / 'apps/agent/openstack_control.py')
    start = line.index('command="') + len('command="')
    end = line.index('"', start)
    line = line[:start] + fixed + line[end:]
    key_path = CLI_HOME / 'authorized_keys'
    account = pwd.getpwnam(CLI_USER)
    # The account owns its directory. Never follow a replaceable destination
    # symlink while writing as root; rename replaces the directory entry itself.
    fd, temporary = tempfile.mkstemp(prefix='.authorized-', dir=CLI_HOME)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(line + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
        os.replace(temporary, key_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    text = ('Port 2222\nListenAddress ' + address.split('/')[0] + '\n'
            'HostKey ' + str(CONFIG / 'runtime_host_key') + '\n'
            'PidFile /run/railshot-personal-runtime.pid\n'
            'AuthorizedKeysFile ' + str(CLI_HOME / 'authorized_keys') + '\n'
            'PermitRootLogin no\nAllowUsers railshot-openstack\nPasswordAuthentication no\n'
            'KbdInteractiveAuthentication no\nPubkeyAuthentication yes\nAuthenticationMethods publickey\nUsePAM no\n'
            'PermitTTY no\nForceCommand ' + fixed + '\n'
            'AllowTcpForwarding no\nAllowAgentForwarding no\nX11Forwarding no\nPermitTunnel no\n'
            'PermitUserEnvironment no\nPermitUserRC no\n')
    atomic_private_write(CONFIG / 'runtime_sshd_config', text.encode())
    run(['/usr/sbin/sshd', '-t', '-f', str(CONFIG / 'runtime_sshd_config')])
    content = (MARKER + '[Unit]\nDescription=RailShot project OpenStack CLI access\nAfter=wg-quick@railshot0.service\n'
               '[Service]\nType=simple\nExecStart=/usr/sbin/sshd -D -e -f ' + str(CONFIG / 'runtime_sshd_config')
               + '\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n')
    require(not RUNTIME_UNIT.is_symlink(), 'SERVICE_NOT_OWNED')
    if RUNTIME_UNIT.exists():
        require(not RUNTIME_UNIT.is_symlink() and RUNTIME_UNIT.read_text().startswith(MARKER), 'SERVICE_NOT_OWNED')
    RUNTIME_UNIT.write_text(content)
    RUNTIME_UNIT.chmod(0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'railshot-personal-runtime'])


def wait_control_ready(config, transport=api, checker=checks, sleeper=time.sleep, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = transport(config['api_url'], '/api/v1/targets/' + config['target_id'] + '/heartbeats',
                             config['client_token'], {'generation': config['generation'], 'client_version': VERSION,
                             'project_id': config['project_id'], 'checks': checker(config), 'capabilities': CAPABILITIES})
        if response.get('status') == 'ready':
            return
        require(response.get('status') not in ('deleted', 'deleting', 'attention'), 'CONTROL_CONNECTION_FAILED')
        sleeper(5)
    raise ValueError('CONTROL_CONNECTION_UNVERIFIED')


def install_service():
    require(ROOT == PAYLOAD, 'PAYLOAD_LOCATION_INVALID')
    content = (MARKER + '[Unit]\nDescription=RailShot personal environment client\nAfter=network-online.target wg-quick@railshot0.service\n'
               '[Service]\nType=simple\nExecStart=' + str(PAYLOAD / '.venv/bin/python') + ' ' + str(PAYLOAD / 'apps/agent/personal.py')
               + ' daemon\nRestart=on-failure\nRestartSec=15\nUMask=0077\nNoNewPrivileges=yes\nProtectHome=yes\n'
               '[Install]\nWantedBy=multi-user.target\n')
    require(not UNIT.is_symlink(), 'SERVICE_NOT_OWNED')
    if UNIT.exists():
        require(not UNIT.is_symlink() and UNIT.read_text().startswith(MARKER), 'SERVICE_NOT_OWNED')
    UNIT.write_text(content)
    UNIT.chmod(0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'railshot-personal'])


def receipt(config, result):
    return api(config['api_url'], '/api/v1/targets/' + config['target_id'] + '/receipts', config['client_token'], result)


def handle_command(config, command, sender=receipt, launch=None):
    require(isinstance(command, dict) and IDENT.fullmatch(command.get('id', '')), 'COMMAND_INVALID')
    require(command.get('kind') == 'environment.delete' and command.get('applications') == [], 'APPLICATIONS_NOT_REMOVED')
    require(command.get('generation', config['generation']) == config['generation'], 'GENERATION_MISMATCH')
    require(command.get('delete_data') in (False, True), 'COMMAND_INVALID')
    job_path = STATE / ('job-' + command['id'] + '.json')
    fingerprint = hashlib.sha256(json.dumps(command, sort_keys=True).encode()).hexdigest()
    if job_path.exists():
        job = json.loads(read_private(job_path))
        require(job['fingerprint'] == fingerprint, 'JOB_ID_CONFLICT')
        # After process interruption we cannot infer whether destructive effects
        # happened. Never replay a removal merely because its receipt is missing.
        sender(config, {'operation_id': command['id'], 'generation': config['generation'], 'status': 'unknown',
                        'steps': [], 'residuals': ['client_removal_unverified'], 'client_removed': False})
        return
    atomic_private_write(job_path, json.dumps({'fingerprint': fingerprint, 'status': 'removing'}).encode())
    sender(config, {'operation_id': command['id'], 'generation': config['generation'], 'status': 'running',
                    'steps': [], 'residuals': ['client'], 'client_removed': False})
    (launch or launch_removal)(config, command['id'])


def launch_removal(config, operation_id):
    require(IDENT.fullmatch(operation_id))
    source = Path(__file__).with_name('personal_remove.py')
    target = Path('/run') / ('railshot-remove-' + operation_id + '.py')
    secret = target.with_suffix('.json')
    require(not target.exists() and not secret.exists(), 'REMOVAL_ALREADY_STARTED')
    # A stdlib-only helper outlives the installed Python environment and removes
    # its /run files before sending the final HTTPS receipt with in-memory auth.
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(source.read_bytes())
    fd = os.open(secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({**config, 'operation_id': operation_id}, stream)
    run(['systemd-run', '--unit=railshot-remove-' + operation_id, '--collect', '--property=Type=exec',
         '/usr/bin/python3', str(target), str(secret)])


class HostMetrics:
    """Management-host measurements only; unreadable values remain null."""
    def __init__(self, proc=Path('/proc'), clock=time.monotonic, disk=shutil.disk_usage):
        self.proc, self.clock, self.disk = proc, clock, disk
        self.previous = None

    def collect(self):
        result = {key: None for key in ('cpu_percent', 'memory_percent', 'disk_percent',
                  'network_receive_bytes_per_second', 'network_transmit_bytes_per_second')}
        current = {'time': self.clock()}
        try:
            values = [int(v) for v in (self.proc / 'stat').read_text().splitlines()[0].split()[1:9]]
            require(len(values) >= 5)
            current['cpu'] = (sum(values), values[3] + values[4])
            if self.previous and 'cpu' in self.previous:
                total = current['cpu'][0] - self.previous['cpu'][0]
                idle = current['cpu'][1] - self.previous['cpu'][1]
                if total > 0 and 0 <= idle <= total:
                    result['cpu_percent'] = round(100 * (total - idle) / total, 2)
        except Exception:
            pass
        try:
            memory = {row.split(':')[0]: int(row.split()[1]) for row in (self.proc / 'meminfo').read_text().splitlines()}
            total, available = memory['MemTotal'], memory['MemAvailable']
            if total > 0 and 0 <= available <= total:
                result['memory_percent'] = round(100 * (total - available) / total, 2)
        except Exception:
            pass
        try:
            usage = self.disk('/')
            if usage.total > 0:
                result['disk_percent'] = round(100 * usage.used / usage.total, 2)
        except Exception:
            pass
        try:
            received = transmitted = 0
            for row in (self.proc / 'net/dev').read_text().splitlines()[2:]:
                name, values = row.split(':', 1)
                if name.strip() == 'lo':
                    continue
                columns = values.split()
                received += int(columns[0])
                transmitted += int(columns[8])
            current['network'] = (received, transmitted)
            if self.previous and 'network' in self.previous and current['time'] > self.previous['time']:
                for index, key in enumerate(('network_receive_bytes_per_second', 'network_transmit_bytes_per_second')):
                    difference = current['network'][index] - self.previous['network'][index]
                    if difference >= 0:
                        result[key] = round(difference / (current['time'] - self.previous['time']), 2)
        except Exception:
            pass
        self.previous = current
        return result


def daemon():
    config = json.loads(read_private(CONFIG / 'client.json'))
    private_directory(STATE)
    metrics = HostMetrics()
    fd = os.open(STATE / 'daemon.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                response = api(config['api_url'], '/api/v1/targets/' + config['target_id'] + '/heartbeats',
                               config['client_token'], {'generation': config['generation'], 'client_version': VERSION,
                               'project_id': config['project_id'], 'checks': checks(config), 'capabilities': CAPABILITIES,
                               'metrics': metrics.collect()})
                if response.get('command'):
                    handle_command(config, response['command'])
            except Exception as exc:
                # Deliberately omit HTTP bodies, commands, credentials and exception messages.
                print('RailShot client operation failed: ' + type(exc).__name__, file=sys.stderr, flush=True)
            time.sleep(30)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('enroll')
    install.add_argument('--api-url', required=True)
    install.add_argument('--enrollment-id', required=True)
    install.add_argument('--profile-id')
    install.add_argument('--project-id')
    commands.add_parser('daemon')
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, 'ROOT_REQUIRED')
        enroll(args) if args.command == 'enroll' else daemon()
    except Exception as exc:
        print('RailShot 작업 실패: ' + type(exc).__name__ + '. 비밀정보를 보호하기 위해 원문 오류를 숨깁니다.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
