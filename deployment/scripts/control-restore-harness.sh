#!/usr/bin/env bash
# Validates/decrypts backup inputs into a NEW directory; Vault restore is explicit.
set -euo pipefail
[[ $# -eq 3 ]] || { echo 'usage: control-restore-harness.sh BUNDLE KEYRING NEW_PRIVATE_TEST_DIRECTORY' >&2; exit 64; }
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
exec /opt/railshot-control/bin/python3 "$script_dir/control_backup.py" restore --bundle "$1" --keyring "$2" --output "$3"
