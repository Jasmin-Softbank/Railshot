"""설치 소유 설정만 관리하며 외부 입력을 셸로 전달하지 않습니다."""
import ipaddress
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import tempfile
import time
from urllib.request import build_opener, HTTPSHandler, ProxyHandler

from .enrollment import NoRedirect, validate_key, validate_registration

MARKER = '# Managed by jasmin bootstrap v1\n'


def _run(args, runner=subprocess.run, *, input=None):
    try:
        result = runner(args, input=input, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('WireGuard 명령 실행에 실패했습니다.') from None
    if result.returncode:
        raise RuntimeError('WireGuard 명령 실행에 실패했습니다.')
    return result.stdout.strip()


def generate_local_wireguard_keypair(*, runner=subprocess.run):
    private = validate_key(_run(['wg', 'genkey'], runner))
    public = validate_key(_run(['wg', 'pubkey'], runner, input=private + '\n'))
    return private, public


def _write_private(path, text):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError('심볼릭 링크에는 설치 설정을 저장하지 않습니다.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_uid != os.geteuid() or path.parent.stat().st_mode & 0o022:
        raise RuntimeError('설정 디렉터리는 실행 계정 소유이며 다른 사용자가 쓸 수 없어야 합니다.')
    fd, temporary = tempfile.mkstemp(prefix='.jasmin-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_private(path):
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1 or metadata.st_mode & 0o077):
        raise RuntimeError('비밀 설정은 실행 계정 소유의 단일 일반 파일이며 소유자만 접근해야 합니다.')
    return path.read_text()


def ensure_keypair(directory, *, runner=subprocess.run):
    path = Path(directory) / 'wireguard-private.key'
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError('개인키 심볼릭 링크를 허용하지 않습니다.')
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise RuntimeError('개인키 파일은 소유자만 접근할 수 있어야 합니다.')
        private = validate_key(_read_private(path).strip())
        return private, validate_key(_run(['wg', 'pubkey'], runner, input=private + '\n'))
    private, public = generate_local_wireguard_keypair(runner=runner)
    _write_private(path, private + '\n')
    return private, public


def _interface(config_path):
    path = Path(config_path)
    if path.name != 'jasmin0.conf':
        raise ValueError('WireGuard 설정 파일 이름은 jasmin0.conf여야 합니다.')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError('WireGuard 설정 심볼릭 링크를 허용하지 않습니다.')
    return 'jasmin0'


def render_config(registration, private_key):
    data = validate_registration(registration)
    validate_key(private_key)
    return (MARKER + '[Interface]\nPrivateKey = ' + private_key + '\nAddress = ' + data['address']
            + '\n\n[Peer]\nPublicKey = ' + data['server_public_key'] + '\nEndpoint = ' + data['endpoint']
            + '\nAllowedIPs = ' + ', '.join(data['allowed_ips']) + '\nPersistentKeepalive = 25\n')


def check_route_conflicts(registration, *, runner=subprocess.run):
    data = validate_registration(registration)
    target = [ipaddress.ip_network(item) for item in data['allowed_ips']]
    target.append(ipaddress.ip_interface(data['address']).network)
    for family in ('-4', '-6'):
        try:
            routes = json.loads(_run(['ip', '-j', family, 'route', 'show'], runner))
        except (ValueError, TypeError):
            raise RuntimeError('기존 경로 정보를 해석하지 못했습니다.') from None
        for route in routes:
            destination = route.get('dst', 'default')
            if destination == 'default' or route.get('dev') == 'jasmin0':
                continue
            network = ipaddress.ip_network(destination, strict=False)
            if any(network.version == n.version and network.overlaps(n) for n in target):
                raise RuntimeError('고객 네트워크와 터널 경로가 겹칩니다.')


def verify_tunnel(registration, *, runner=subprocess.run, opener=None, attempts=5, sleep=time.sleep):
    data = validate_registration(registration)
    opener = opener or build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    for attempt in range(attempts):
        try:
            with opener.open(data['probe_url'], timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError('점검 요청 실패')
            rows = _run(['wg', 'show', 'jasmin0', 'latest-handshakes'], runner).splitlines()
            now = int(time.time())
            if any(len(row.split()) == 2 and row.split()[0] == data['server_public_key']
                   and 0 <= now - int(row.split()[1]) <= 180 for row in rows):
                return {'connected': True, 'interface': 'jasmin0'}
        except Exception:
            pass
        if attempt + 1 < attempts:
            sleep(2)
    raise RuntimeError('WireGuard 터널 내부 HTTPS 연결과 최근 연결 응답을 확인하지 못했습니다.')


def configure_wireguard(registration, private_key, config_path, *, runner=subprocess.run, verifier=None):
    path = Path(config_path)
    _interface(path)
    if runner is subprocess.run and path != Path('/etc/wireguard/jasmin0.conf'):
        raise ValueError('실제 설치 설정 경로는 /etc/wireguard/jasmin0.conf로 고정됩니다.')
    content = render_config(registration, private_key)
    if path.exists():
        if _read_private(path) != content:
            raise RuntimeError('기존 WireGuard 설정은 자동으로 덮어쓰지 않습니다.')
        if path.stat().st_mode & 0o077:
            raise RuntimeError('WireGuard 설정 접근 권한이 안전하지 않습니다.')
        _run(['systemctl', 'start', 'wg-quick@jasmin0'], runner)
        (verifier or verify_tunnel)(registration, runner=runner)
        _run(['systemctl', 'enable', 'wg-quick@jasmin0'], runner)
        return {'interface': 'jasmin0', 'reused': True}
    check_route_conflicts(registration, runner=runner)
    active = runner(['ip', 'link', 'show', 'jasmin0'], capture_output=True, text=True, timeout=10, check=False)
    if active.returncode == 0:
        raise RuntimeError('기존 jasmin0 인터페이스를 사용하지 않습니다.')
    _write_private(path, content)
    try:
        _run(['systemctl', 'start', 'wg-quick@jasmin0'], runner)
        (verifier or verify_tunnel)(registration, runner=runner)
        _run(['systemctl', 'enable', 'wg-quick@jasmin0'], runner)
    except Exception:
        try:
            _run(['systemctl', 'disable', '--now', 'wg-quick@jasmin0'], runner)
        except RuntimeError:
            raise RuntimeError('터널 설정에 실패했고 복구도 실패했습니다. 설정을 보존했습니다.') from None
        path.unlink()
        raise RuntimeError('터널 설정 실패로 이번 설치 설정을 복구했습니다.') from None
    return {'interface': 'jasmin0', 'reused': False}


def uninstall_wireguard(config_path, *, runner=subprocess.run):
    path = Path(config_path)
    _interface(path)
    if runner is subprocess.run and path != Path('/etc/wireguard/jasmin0.conf'):
        raise ValueError('실제 설치 설정 경로는 /etc/wireguard/jasmin0.conf로 고정됩니다.')
    if not path.exists():
        return {'removed': False}
    if not _read_private(path).startswith(MARKER):
        raise RuntimeError('이 설치가 소유하지 않은 WireGuard 설정입니다.')
    _run(['systemctl', 'disable', '--now', 'wg-quick@jasmin0'], runner)
    path.unlink()
    return {'removed': True}
