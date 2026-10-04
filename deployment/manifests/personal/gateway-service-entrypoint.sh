#!/bin/sh
set -eu
umask 077

: "${PERSONAL_GATEWAY_ENDPOINT:?set the reviewed public HOST:51820 endpoint}"
socket_dir=/run/railshot-personal-gateway
config=/var/lib/railshot-personal-gateway/config.json
mkdir -p "$socket_dir"
chmod 0770 "$socket_dir"

python3 /opt/railshot/deployment/scripts/personal_gateway_service.py \
  --initialize --config "$config" --endpoint "$PERSONAL_GATEWAY_ENDPOINT"
python3 /opt/railshot/deployment/scripts/personal_wireguard.py \
  --config "$config" --restore >/dev/null
exec python3 /opt/railshot/deployment/scripts/personal_gateway_service.py \
  --serve --config "$config"
