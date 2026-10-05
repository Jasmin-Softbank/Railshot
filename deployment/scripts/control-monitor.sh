#!/usr/bin/env bash
# Local health signal only. A service failure is delivered to the system journal.
set -euo pipefail
[[ $# -eq 4 ]] || { echo 'usage: control-monitor.sh PROVISION_CONFIG CUSTODY_DATA KEYRING LATEST_BACKUP_BUNDLE' >&2; exit 64; }
script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
/opt/railshot-control/bin/python3 "$script_dir/control_vault.py" monitor --config "$1"
/opt/railshot-control/bin/python3 "$script_dir/recovery_service.py" --data-dir "$2" --keyring "$3" ready
/opt/railshot-control/bin/python3 - "$2" "$4" <<'PY'
import os,shutil,stat,sys,time
from pathlib import Path
try:
 for item in sys.argv[1:]:
  info=Path(item).lstat()
  if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077: raise ValueError()
 usage=shutil.disk_usage(sys.argv[1])
 if usage.used/usage.total>=0.85: raise ValueError()
 manifest=Path(sys.argv[2])/'manifest.json'; info=manifest.lstat()
 if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o077 or time.time()-info.st_mtime>86400: raise ValueError()
 print('{"disk_below_85_percent":true,"backup_manifest_younger_than_24_hours":true}')
except Exception:
 print('{"error":"CONTROL_MONITOR_FAILED"}',file=sys.stderr); raise SystemExit(3)
PY
