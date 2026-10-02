#!/bin/bash
set -euo pipefail
umask 077
if [ "${1:-}" = --check-image ]; then
  exec python3 /opt/railshot/ci/scripts/runner/container_preflight.py --check-image
fi
if [ "$#" -ne 0 ]; then
  echo 'Supported argument: --check-image; omit to start the ephemeral runner.' >&2
  exit 2
fi
: "${RAILSHOT_RUNNER_URL:?Set the private apps repository URL}"
: "${RAILSHOT_RUNNER_NAME:?Set a unique runner name for this invocation}"
: "${RAILSHOT_RUNNER_LABELS:?Set the labels selected by RAILSHOT_CI_RUNNER_LABELS}"
# One job owns the same-path workspace and Docker builder on each worker.
exec 9>/var/lib/railshot-runner/work/.runner.lock
flock -n 9 || { echo 'This build worker already has an active runner.' >&2; exit 2; }
python3 /opt/railshot/ci/scripts/runner/container_preflight.py
# This creates local Buildx connection metadata only. The existing Ansible
# bootstrap owns the BuildKit container, resource bounds and network policy.
builder=$(jq -er '.builder.name' /etc/railshot/ci-executor.yaml)
container=$(jq -er '.builder.container' /etc/railshot/ci-executor.yaml)
docker buildx create --name "$builder" --driver remote "docker-container://$container" >/dev/null
python3 /opt/railshot/ci/scripts/runner/container_preflight.py --check-builder
mkdir -p "$TMPDIR"
token=$(cat /run/secrets/github-runner-registration-token)
if [ -z "$token" ]; then echo 'Empty runner registration token file.' >&2; exit 2; fi
./config.sh --unattended --ephemeral --disableupdate \
  --url "$RAILSHOT_RUNNER_URL" --token "$token" \
  --name "$RAILSHOT_RUNNER_NAME" --labels "$RAILSHOT_RUNNER_LABELS" \
  --work /var/lib/railshot-runner/work
unset token
exec ./run.sh
