#!/usr/bin/env bash
# Download a pinned artifact, prepare local project authentication, then enroll.
set +x
set -euo pipefail
umask 077
ENROLLMENT_SECRET="${RAILSHOT_ENROLLMENT_TOKEN:-}"
unset RAILSHOT_ENROLLMENT_TOKEN
API_URL=''
ENROLLMENT_ID=''
ARTIFACT_URL=''
ARTIFACT_SHA256=''
PROFILE_ID=''
SOURCE_ROOT=''
PROJECT_ID=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        --api-url|--enrollment-id|--artifact-url|--artifact-sha256|--profile-id|--source-root|--project-id)
            [[ $# -ge 2 ]] || { echo '인자 값이 필요합니다.' >&2; exit 1; }
            case "$1" in
                --api-url) API_URL="$2" ;;
                --enrollment-id) ENROLLMENT_ID="$2" ;;
                --artifact-url) ARTIFACT_URL="$2" ;;
                --artifact-sha256) ARTIFACT_SHA256="$2" ;;
                --profile-id) PROFILE_ID="$2" ;;
                --source-root) SOURCE_ROOT="$2" ;;
                --project-id) PROJECT_ID="$2" ;;
            esac
            shift 2 ;;
        --help) echo 'Ubuntu 24.04: sudo bash personal-install.sh --api-url HTTPS_URL --enrollment-id ID --artifact-url HTTPS_TGZ --artifact-sha256 SHA256 [--profile-id ID]'; exit 0 ;;
        *) echo '지원하지 않는 인자입니다.' >&2; exit 1 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo 'root 권한이 필요합니다.' >&2; exit 1; }
[[ -n "$API_URL" && "$ENROLLMENT_ID" =~ ^[A-Za-z0-9_-]{1,128}$ ]] || { echo '등록 인자가 올바르지 않습니다.' >&2; exit 1; }
python3 - "$API_URL" "$ARTIFACT_URL" "$SOURCE_ROOT" <<'PY'
from pathlib import Path
import sys
from urllib.parse import urlsplit
values = dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
if values.get('ID','').strip('"') != 'ubuntu' or values.get('VERSION_ID','').strip('"') != '24.04':
    raise SystemExit('Ubuntu 24.04만 지원합니다.')
for value in ([sys.argv[1]] if sys.argv[3] else sys.argv[1:3]):
    url = urlsplit(value)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or any(c.isspace() for c in value):
        raise SystemExit('인증정보 및 질의문자열 없는 HTTPS 주소가 필요합니다.')
PY
if [[ -z "$SOURCE_ROOT" ]]; then
    [[ "$ARTIFACT_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] || { echo '고정 배포본 SHA256 검증값이 필요합니다.' >&2; exit 1; }
fi
WORK_DIR="$(mktemp -d /tmp/railshot-personal.XXXXXXXX)"
trap 'rm -rf -- "$WORK_DIR"' EXIT
# This lock remains outside installation-owned paths; no concurrent install/remove.
exec 9>/run/railshot-personal-install.lock
flock -n 9 || { echo '설치 작업이 이미 실행 중입니다.' >&2; exit 1; }
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl wireguard-tools iproute2 openssh-server python3-venv python3-openstackclient
if [[ -z "$SOURCE_ROOT" ]]; then
    STATUS="$(curl --disable --fail --silent --show-error --proto '=https' --max-redirs 0 --connect-timeout 15 --max-time 300 --output "$WORK_DIR/payload.tgz" --write-out '%{http_code}' "$ARTIFACT_URL")"
    [[ "$STATUS" == 200 ]] || { echo '배포본 다운로드 실패입니다.' >&2; exit 1; }
    python3 - "$WORK_DIR" "$ARTIFACT_SHA256" <<'PY'
import hashlib, sys, tarfile
from pathlib import Path
root=Path(sys.argv[1]); archive=root/'payload.tgz'
if hashlib.sha256(archive.read_bytes()).hexdigest() != sys.argv[2].lower():
    raise SystemExit('배포본 검증값 불일치입니다.')
with tarfile.open(archive) as source:
    members=source.getmembers()
    if any(m.issym() or m.islnk() or m.isdev() or not (m.isfile() or m.isdir()) or m.name.startswith('/') or '..' in Path(m.name).parts for m in members):
        raise SystemExit('안전하지 않은 배포본입니다.')
    source.extractall(root/'source', filter='data')
PY
    SOURCE_ROOT="$WORK_DIR/source"
fi
python3 - "$SOURCE_ROOT" "$ARTIFACT_SHA256" <<'PY'
import hashlib,json,os,shutil,sys,tempfile
from pathlib import Path
source=Path(sys.argv[1]); target=Path('/opt/railshot/personal')
required=['apps/agent/personal.py','apps/agent/personal_remove.py','apps/agent/openstack_control.py','deployment/bootstrap/client_setup/state.py','infrastructure/providers/openstack/cli.py']
if any(not (source/p).is_file() for p in required):
    raise SystemExit('배포본 루트 또는 필수 파일이 올바르지 않습니다.')
for p in (target,*target.parents):
    if p.is_symlink() or p.exists() and (p.stat().st_uid != 0 or p.stat().st_mode & 0o022):
        raise SystemExit('설치 경로 소유권 또는 권한이 안전하지 않습니다.')
files={}
for tree in ('apps/agent','deployment/bootstrap/client_setup','infrastructure/providers/openstack'):
    for p in (source/tree).rglob('*'):
        if any(x in ('__pycache__','tests','.venv') for x in p.parts): continue
        if p.is_symlink(): raise SystemExit('심볼릭 링크 배포본은 허용하지 않습니다.')
        if p.is_file(): files[str(p.relative_to(source))]=hashlib.sha256(p.read_bytes()).hexdigest()
record={'version':1,'files':files}
marker=target/'.railshot-personal.json'
if target.exists():
    if not marker.is_file() or json.loads(marker.read_text()) != record:
        raise SystemExit('다른 설치/버전을 덮어쓰지 않습니다.')
    if any(not (target/p).is_file() or hashlib.sha256((target/p).read_bytes()).hexdigest()!=digest for p,digest in files.items()):
        raise SystemExit('기존 설치의 무결성을 확인하지 못했습니다.')
else:
    created_parent = not target.parent.exists()
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
    if created_parent: target.parent.chmod(0o755)
    elif not target.parent.stat().st_mode & 0o001:
        raise SystemExit('기존 코드 상위 경로를 클라이언트 계정이 통과할 수 없습니다. 기존 권한은 변경하지 않습니다.')
    staging=Path(tempfile.mkdtemp(prefix='.personal-',dir=target.parent))
    try:
        for p in files:
            dest=staging/p; dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/p,dest); dest.chmod(0o644)
        (staging/'.railshot-personal.json').write_text(json.dumps(record,sort_keys=True))
        staging.rename(target)
    finally:
        if staging.exists(): shutil.rmtree(staging)
PY
python3 -m venv /opt/railshot/personal/.venv
/opt/railshot/personal/.venv/bin/python -m pip install 'cryptography==50.0.2'
# Code is public; credentials remain in separate 0700 directories.
chmod -R a+rX /opt/railshot/personal
ARGS=(enroll --api-url "$API_URL" --enrollment-id "$ENROLLMENT_ID")
[[ -z "$PROFILE_ID" ]] || ARGS+=(--profile-id "$PROFILE_ID")
[[ -z "$PROJECT_ID" ]] || ARGS+=(--project-id "$PROJECT_ID")
RAILSHOT_ENROLLMENT_TOKEN="$ENROLLMENT_SECRET" /opt/railshot/personal/.venv/bin/python /opt/railshot/personal/apps/agent/personal.py "${ARGS[@]}"
unset ENROLLMENT_SECRET
echo 'RailShot 등록 및 클라이언트 서비스 설치를 마쳤습니다. 서버에서의 실제 터널·OpenStack 명령 검증 결과는 관리 화면에서 확인하십시오.'
