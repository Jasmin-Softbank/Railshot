#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
kubectl apply -f "$ROOT_DIR/manifests/deployment.yaml"
kubectl apply -f "$ROOT_DIR/manifests/service.yaml"
kubectl -n jasmin-poc rollout status deployment/jasmin-sample --timeout="$WAIT_TIMEOUT"
kubectl -n jasmin-poc get deployment,pods,service,endpointslices -o wide
