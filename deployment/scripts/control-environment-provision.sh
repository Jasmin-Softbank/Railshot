#!/usr/bin/env bash
# Root-only registered config. No caller-selected Vault operation or secret argv.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
if [[ $# -eq 3 && $1 != --* ]]; then
  exec /opt/railshot-control/bin/python3 "$script_dir/control_vault.py" provision --environment-id "$1" --config "$2" --output-dir "$3"
fi
exec /opt/railshot-control/bin/python3 "$script_dir/control_vault.py" provision "$@"
