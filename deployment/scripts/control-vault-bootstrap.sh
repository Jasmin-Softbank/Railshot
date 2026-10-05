#!/usr/bin/env bash
# Initial central Shamir shares and root token are PGP encrypted by Vault itself.
set -euo pipefail
[[ $# -eq 4 && $1 == https://* && $2 == /* && $3 == /* && $4 == /* ]] || { echo 'usage: control-vault-bootstrap.sh ADDRESS CA_FILE PGP_RECIPIENT_PATHS_FILE ROOT_TOKEN_PGP_PUBLIC_KEY_FILE' >&2; exit 64; }
address=$1; ca=$2; recipients_file=$3; root_recipient=$4
recipients=$(python3 - "$ca" "$recipients_file" "$root_recipient" <<'PY'
import base64, binascii, os, stat, subprocess, sys
from pathlib import Path
def private(path):
 info=os.lstat(path)
 if not Path(path).is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077: raise ValueError()
 return Path(path).read_bytes()
try:
 private(sys.argv[1]); entries=private(sys.argv[2]).decode().splitlines(); private(sys.argv[3])
 if len(entries)!=5 or any(not item or ',' in item for item in entries): raise ValueError()
 fingerprints=[]
 for item in entries+[sys.argv[3]]:
  raw=private(item)
  try: binary=base64.b64decode(raw.strip(),validate=True)
  except (ValueError,binascii.Error): binary=raw
  result=subprocess.run(['gpg','--batch','--with-colons','--show-keys'],input=binary,capture_output=True,check=True)
  lines=result.stdout.decode().splitlines()
  if sum(line.startswith('pub:') for line in lines)!=1 or any(line.startswith('sec:') for line in lines): raise ValueError()
  fingerprint=next(line.split(':')[9] for line in lines if line.startswith('fpr:'))
  fingerprints.append(fingerprint)
 if len(set(fingerprints[:5]))!=5: raise ValueError()
 print(','.join(entries))
except Exception:
 print('invalid private public-key inputs',file=sys.stderr); raise SystemExit(65)
PY
)
export VAULT_ADDR="$address" VAULT_CACERT="$ca"
status_file=$(mktemp); result_file=$(mktemp); chmod 0600 "$status_file" "$result_file"
trap 'rm -f "$status_file" "$result_file"' EXIT
rc=0; vault status -format=json >"$status_file" 2>/dev/null || rc=$?
[[ $rc -eq 2 ]] || { echo 'Vault status unavailable or already ready; initialization refused' >&2; exit 70; }
python3 - "$status_file" <<'PY'
import json,sys
v=json.load(open(sys.argv[1])); assert v.get('initialized') is False and v.get('sealed') is True
PY
# The flag takes a path to a public PGP key, not the file's literal contents.
vault operator init -key-shares=5 -key-threshold=3 -pgp-keys="$recipients" -root-token-pgp-key="$root_recipient" -format=json >"$result_file" 2>/dev/null
python3 - "$result_file" <<'PY'
import base64,json,sys
v=json.load(open(sys.argv[1])); keys=v['unseal_keys_b64']
assert len(keys)==5 and all(len(base64.b64decode(k,validate=True))>100 for k in keys)
assert len(base64.b64decode(v['root_token'],validate=True))>100
print(json.dumps(v,separators=(',',':')))
PY
