#!/usr/bin/env bash
# Reports readiness only after both control services are running and healthy.
set -euo pipefail

[[ $# -eq 1 && $1 == /* ]] || {
  echo 'usage: control-health.sh /absolute/private/env-file' >&2
  exit 64
}

python3 - "$1" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
try:
    metadata = os.lstat(path)
except OSError as error:
    print(f"private env file is unavailable: {error}", file=sys.stderr)
    raise SystemExit(65)

if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
    print("private env file must be an owner-only regular file", file=sys.stderr)
    raise SystemExit(65)
PY

docker compose --env-file "$1" -f deployment/control-compose.yaml ps --format json |
  python3 -c '
import json
import sys

raw = sys.stdin.read().strip()
try:
    rows = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
except json.JSONDecodeError as error:
    print(f"invalid docker compose status output: {error}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(rows, list):
    print("docker compose status must be a JSON array or line-delimited objects", file=sys.stderr)
    raise SystemExit(1)

by_service = {}
for row in rows:
    if not isinstance(row, dict) or not isinstance(row.get("Service"), str):
        print("docker compose status is missing a service name", file=sys.stderr)
        raise SystemExit(1)
    by_service[row["Service"]] = row

for service in ("vault", "custody"):
    row = by_service.get(service)
    if row is None:
        print(f"{service} container is missing", file=sys.stderr)
        raise SystemExit(1)
    if str(row.get("State", "")).lower() != "running":
        print(f"{service} container is not running", file=sys.stderr)
        raise SystemExit(1)
    if str(row.get("Health", "")).lower() != "healthy":
        print(f"{service} container is not healthy", file=sys.stderr)
        raise SystemExit(1)

print("control Vault and custody are ready")
'
