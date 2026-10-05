#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 5 ]] || { echo 'usage: control-backup.sh SNAPSHOT CUSTODY_SQLITE KEYRING REFERENCE_JSON NEW_PRIVATE_DESTINATION' >&2; exit 64; }
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
exec /opt/railshot-control/bin/python3 "$script_dir/control_backup.py" backup --snapshot "$1" --custody "$2" --keyring "$3" --reference "$4" --output "$5"
