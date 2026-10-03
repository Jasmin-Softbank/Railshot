#!/usr/bin/env bash
set +x
set -euo pipefail
# 폐기한 등록 키를 하위 프로세스에 전달하지 않습니다.
unset RAILSHOT_ENROLLMENT_TOKEN JASMIN_ENROLLMENT_TOKEN
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_RUN=false
INSTALL_DEPS=false
DOWNLOAD_BASE_URL="${JASMIN_DOWNLOAD_BASE_URL:-}"
DOWNLOAD_DIR=''
cleanup_download() {
    local status=$?
    if [[ -n "$DOWNLOAD_DIR" ]]; then rm -rf -- "$DOWNLOAD_DIR"; fi
    return "$status"
}
trap cleanup_download EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-run) SOURCE_RUN=true; shift ;;
        --install-dependencies) INSTALL_DEPS=true; shift ;;
        --download-base-url)
            [[ $# -ge 2 && -n "$2" ]] || { echo '--download-base-url에는 HTTPS 배포 주소가 필요합니다.' >&2; exit 1; }
            DOWNLOAD_BASE_URL="$2"; shift 2 ;;
        *) break ;;
    esac
done
# 동일 배포에 필요한 파일 목록입니다. 테스트와 개발용 파일은 내려받지 않습니다.
required_files=(
    deployment/bootstrap/install.sh
    deployment/bootstrap/uninstall.sh
    deployment/bootstrap/install_payload.py
    deployment/bootstrap/requirements.lock
    deployment/bootstrap/client_setup/__init__.py
    deployment/bootstrap/client_setup/main.py
    deployment/bootstrap/client_setup/preflight.py
    deployment/bootstrap/client_setup/wireguard.py
    deployment/bootstrap/client_setup/credentials.py
    deployment/bootstrap/client_setup/state.py
    deployment/bootstrap/client_setup/report.py
    deployment/bootstrap/templates/agent-authorized-keys.README
    infrastructure/providers/openstack/__init__.py
    infrastructure/providers/openstack/cli.py
    infrastructure/providers/openstack/identity.py
    infrastructure/providers/openstack/discovery.py
    infrastructure/providers/openstack/access.py
    infrastructure/providers/openstack/templates/cloud-init.yaml.tmpl
    apps/agent/__init__.py
    apps/agent/install_forced_command.py
    apps/agent/protocol.py
    apps/agent/runner.py
    apps/agent/sender.py
)
if [[ -n "$DOWNLOAD_BASE_URL" ]]; then
    # 인증정보, 질의 문자열, fragment 및 제어문자가 포함된 URL은 받지 않습니다.
    url_pattern='^https://([A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\])(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$'
    [[ "$DOWNLOAD_BASE_URL" =~ $url_pattern ]] || { echo '배포 주소는 인증정보·쿼리·공백 없는 HTTPS 주소여야 합니다.' >&2; exit 1; }
    command -v curl >/dev/null || { echo '배포 파일 다운로드에 curl이 필요합니다.' >&2; exit 1; }
    DOWNLOAD_BASE_URL="${DOWNLOAD_BASE_URL%/}"
    DOWNLOAD_DIR="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/jasmin-bootstrap.XXXXXXXX")"
    chmod 700 "$DOWNLOAD_DIR"
    for relative in "${required_files[@]}"; do
        destination="$DOWNLOAD_DIR/$relative"
        mkdir -p -- "${destination%/*}"
        # curl 설정 파일 및 리다이렉트를 사용하지 않으며 TLS 검증을 유지합니다.
        if ! http_status="$(curl --disable --fail --show-error --silent --proto '=https' \
            --connect-timeout 15 --max-time 120 --max-redirs 0 \
            --output "$destination" --write-out '%{http_code}' \
            "$DOWNLOAD_BASE_URL/$relative")"; then
            echo "배포 파일 다운로드 실패: $relative" >&2
            exit 1
        fi
        if [[ "$http_status" != 200 || ! -f "$destination" ]]; then
            echo "배포 파일 응답 오류: $relative (HTTP $http_status; 200만 허용)" >&2
            exit 1
        fi
    done
    SCRIPT_DIR="$DOWNLOAD_DIR/deployment/bootstrap"
else
    LOCAL_ROOT="$SCRIPT_DIR/../.."
    for relative in "${required_files[@]}"; do
        if [[ ! -f "$LOCAL_ROOT/$relative" ]]; then
            echo "로컬 배포 파일 누락: $relative. --download-base-url 또는 JASMIN_DOWNLOAD_BASE_URL로 HTTPS 배포 루트를 지정하십시오." >&2
            exit 1
        fi
    done
fi
# Validate the shared CLI/config before package, payload or virtualenv changes.
for argument in "$@"; do
    if [[ "$argument" == "--help" || "$argument" == "-h" ]]; then
        PYTHONPATH="$SCRIPT_DIR" python3 -m client_setup.preflight "$@"
        exit 0
    fi
done
PYTHONPATH="$SCRIPT_DIR" python3 -m client_setup.preflight "$@"
if [[ "$INSTALL_DEPS" == true ]]; then
    [[ ${EUID} -eq 0 ]] || { echo '의존성 설치는 root 권한이 필요합니다.' >&2; exit 1; }
    python3 - <<'PY'
from pathlib import Path
values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
if values.get('ID', '').strip('"') != 'ubuntu' or values.get('VERSION_ID', '').strip('"') != '24.04':
    raise SystemExit('Ubuntu 24.04만 지원합니다.')
PY
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl iproute2 openssh-client python3-venv python3-openstackclient
fi
if [[ "$SOURCE_RUN" == false ]]; then
    python3 "$SCRIPT_DIR/install_payload.py"
    SCRIPT_DIR=/opt/jasmin/bootstrap/deployment/bootstrap
fi
PYTHON=python3
if [[ "$SOURCE_RUN" == false || "$INSTALL_DEPS" == true ]]; then
    [[ ! -L "$SCRIPT_DIR/.venv" ]] || { echo '가상환경 심볼릭 링크를 허용하지 않습니다.' >&2; exit 1; }
    python3 - "$SCRIPT_DIR/.venv" <<'PY'
import os, stat, sys
from pathlib import Path
path = Path(sys.argv[1])
if path.exists():
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise SystemExit('기존 가상환경 소유권 또는 접근 권한이 안전하지 않습니다.')
PY
    python3 -m venv "$SCRIPT_DIR/.venv"
    "$SCRIPT_DIR/.venv/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.lock"
fi
if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then PYTHON="${SCRIPT_DIR}/.venv/bin/python"; fi
export PYTHONPATH="${SCRIPT_DIR}"
"$PYTHON" -m client_setup.main "$@"
