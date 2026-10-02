"""지원 호스트의 읽기 전용 사전 점검."""
import os
from pathlib import Path
import shutil
import subprocess


def run_preflight(*, os_release=Path('/etc/os-release'), geteuid=os.geteuid,
                  which=shutil.which, runner=subprocess.run):
    if geteuid() != 0:
        raise RuntimeError('설치는 root 권한이 필요합니다.')
    values = {}
    for line in Path(os_release).read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key] = value.strip('"')
    if values.get('ID') != 'ubuntu' or values.get('VERSION_ID') != '24.04':
        raise RuntimeError('현재 지원 운영체제는 Ubuntu 24.04입니다.')
    required = ('wg', 'wg-quick', 'ip', 'systemctl', 'openstack', 'ssh', 'ssh-keygen')
    missing = [name for name in required if not which(name)]
    if missing:
        raise RuntimeError('필수 명령이 없습니다: ' + ', '.join(missing))
    result = runner(['systemctl', 'is-system-running'], capture_output=True,
                    text=True, timeout=15, check=False)
    if result.stdout.strip() not in ('running', 'degraded'):
        raise RuntimeError('systemd 서비스 관리 환경을 확인하지 못했습니다.')
    return {'os': 'ubuntu', 'version': '24.04', 'commands': list(required)}
