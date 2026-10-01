#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT_DIR/scripts/common.sh"

source "$ROOT_DIR/scripts/preflight.sh"
for script in install-k3s install-cilium deploy-sample verify; do
  STEP=$script
  log "시작: $STEP"
  bash "$ROOT_DIR/scripts/$script.sh"
  log "완료: $STEP"
done
log 'SUCCESS: 단일 노드 runtime, Cilium, nginx Service, 서버 HTTP 200 확인 완료.'
log '서버 밖의 접근은 README의 별도 클라이언트 curl 절차로 확인하세요.'
