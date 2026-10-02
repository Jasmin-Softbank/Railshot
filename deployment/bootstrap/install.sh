#!/usr/bin/env bash
set +x
set -euo pipefail
# 등록 키를 의존성 설치 하위 프로세스에 전달하지 않습니다.
ENROLLMENT_TOKEN="${JASMIN_ENROLLMENT_TOKEN:-}"
unset JASMIN_ENROLLMENT_TOKEN
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_RUN=false
INSTALL_DEPS=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-run) SOURCE_RUN=true; shift ;;
        --install-dependencies) INSTALL_DEPS=true; shift ;;
        *) break ;;
    esac
done
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then SOURCE_RUN=true; fi
if [[ "$INSTALL_DEPS" == true ]]; then
    [[ ${EUID} -eq 0 ]] || { echo '의존성 설치는 root 권한이 필요합니다.' >&2; exit 1; }
    python3 - <<'PY'
from pathlib import Path
values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
if values.get('ID', '').strip('"') != 'ubuntu' or values.get('VERSION_ID', '').strip('"') != '24.04':
    raise SystemExit('Ubuntu 24.04만 지원합니다.')
PY
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends wireguard-tools iproute2 openssh-client python3-venv python3-openstackclient
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
if [[ -n "$ENROLLMENT_TOKEN" ]]; then export JASMIN_ENROLLMENT_TOKEN="$ENROLLMENT_TOKEN"; fi
unset ENROLLMENT_TOKEN
exec "$PYTHON" -m client_setup.main "$@"
