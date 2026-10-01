#!/usr/bin/env bash
# Runtime-only entrypoint: no sample app, CD controller, database or public URL claim.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT_DIR/scripts/common.sh"
source "$ROOT_DIR/scripts/preflight.sh"
for script in install-k3s install-cilium; do
  STEP=$script
  log "시작: $STEP"
  bash "$ROOT_DIR/scripts/$script.sh"
  log "완료: $STEP"
done
log 'Runtime checks passed: K3s API, single Node, Cilium and CoreDNS. App/CD/public HTTP are not checked.'
