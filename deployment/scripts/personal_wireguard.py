#!/usr/bin/env python3
"""Root-owned WireGuard gateway adapter. JSON stdin/stdout; never log key material."""
import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

MARKER = '# Managed by RailShot personal gateway v1\n'
ID = re.compile(r'[A-Za-z0-9_-]{1,128}')


def require(value, code='GATEWAY_INVALID'):
    if not value:
        raise ValueError(code)


def key(value):
    try:
        require(isinstance(value, str) and len(base64.b64decode(value, validate=True)) == 32)
    except Exception:
        raise ValueError('GATEWAY_KEY_INVALID') from None
    return value


def private(path, owner=None, max_bytes=1048576):
    path = Path(path)
    require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)), 'GATEWAY_PATH_UNSAFE')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r') as stream:
        metadata = os.fstat(stream.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == (os.geteuid() if owner is None else owner)
                and not metadata.st_mode & 0o077 and metadata.st_nlink == 1, 'GATEWAY_FILE_UNSAFE')
        result = stream.read(max_bytes + 1)
        require(len(result) <= max_bytes, 'GATEWAY_FILE_TOO_LARGE')
        return result


def save(path, value):
    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'GATEWAY_PATH_UNSAFE')
    if path.exists():
        private(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.parent.stat().st_uid == os.geteuid() and not path.parent.stat().st_mode & 0o022)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(args, *, input=None):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=30, check=False)
    require(result.returncode == 0, 'GATEWAY_COMMAND_FAILED')
    return result.stdout.strip()


def validate(config):
    require(config.get('version') == 1)
    require(re.fullmatch(r'railshot[a-z0-9]{0,7}', config['interface']))
    pool = ipaddress.ip_network(config['address_pool'])
    address = ipaddress.ip_interface(config['server_address'])
    require(pool.version == 4 and 24 <= pool.prefixlen <= 28 and address.ip in pool and address.network.prefixlen == 32)
    require(re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}', config['endpoint']))
    require(1 <= int(config['endpoint'].rsplit(':', 1)[1]) <= 65535)
    require(type(config['listen_port']) is int and 1 <= config['listen_port'] <= 65535)
    return pool, address.ip


def apply(config, ledger, runner):
    interface = config['interface']
    path = Path('/etc/wireguard') / (interface + '.conf')
    secret = key(private(config['private_key_file']).strip())
    lines = [MARKER, '[Interface]\n', 'PrivateKey = ' + secret + '\n',
             'Address = ' + config['server_address'] + '\n', 'ListenPort = ' + str(config['listen_port']) + '\n',
             'PostUp = iptables -w -I FORWARD -i %i -j DROP; iptables -w -I FORWARD -o %i -j DROP\n']
    for peer in ledger.values():
        if not peer.get('removed'):
            lines.extend(['\n[Peer]\n', 'PublicKey = ' + key(peer['public_key']) + '\n', 'AllowedIPs = ' + peer['address'] + '\n'])
    if path.exists():
        require(private(path).startswith(MARKER), 'GATEWAY_CONFIG_NOT_OWNED')
    save(path, ''.join(lines))
    # Block forwarding in both directions before enabling peers: customers receive
    # only a server /32 route and cannot use this gateway as a network transit.
    for direction in ('-i', '-o'):
        rule = ['FORWARD', direction, interface, '-j', 'DROP']
        try:
            runner(['iptables', '-w', '-C', *rule])
        except Exception:
            runner(['iptables', '-w', '-I', *rule])
    runner(['systemctl', 'enable', '--now', 'wg-quick@' + interface])
    stripped = runner(['wg-quick', 'strip', str(path)])
    fd, tmp = tempfile.mkstemp(prefix='railshot-wg-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(stripped)
        runner(['wg', 'syncconf', interface, tmp])
        for peer in ledger.values():
            route = ['ip', '-4', 'route']
            if peer.get('removed'):
                existing = runner([*route, 'show', peer['address'], 'dev', interface])
                if existing:
                    runner([*route, 'del', peer['address'], 'dev', interface])
            else:
                runner([*route, 'replace', peer['address'], 'dev', interface])
    finally:
        os.unlink(tmp)


def execute(config, request, runner=run, apply_fn=apply, now=time.time):
    pool, server_ip = validate(config)
    require(set(request) == {'action', 'target_id', 'generation', 'public_key'})
    require(request['action'] in ('register', 'remove', 'verify') and ID.fullmatch(request['target_id']))
    require(type(request['generation']) is int and request['generation'] > 0)
    public_key = key(request['public_key'])
    ledger_path = Path(config['ledger_path'])
    ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not any(p.is_symlink() for p in (ledger_path.parent, *ledger_path.parent.parents)))
    lock = ledger_path.with_suffix('.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private(lock)
        fcntl.flock(fd, fcntl.LOCK_EX)
        ledger = json.loads(private(ledger_path)) if ledger_path.exists() else {}
        ident = request['target_id'] + ':' + str(request['generation'])
        peer = ledger.get(ident)
        if peer:
            require(peer['public_key'] == public_key, 'GATEWAY_PEER_CONFLICT')
        action = request['action']
        if action == 'register':
            require(not peer or not peer.get('removed'), 'GATEWAY_PEER_REVOKED')
            require(not any(k != ident and p['public_key'] == public_key for k, p in ledger.items()), 'GATEWAY_KEY_REUSED')
            if not peer:
                used = {p['address'] for p in ledger.values()}  # tombstones are never reused
                address = next((str(ip) + '/32' for ip in pool.hosts() if ip != server_ip and str(ip) + '/32' not in used), None)
                require(address, 'GATEWAY_POOL_EXHAUSTED')
                peer = {'public_key': public_key, 'address': address, 'removed': False}
                ledger[ident] = peer
                save(ledger_path, json.dumps(ledger, sort_keys=True))
            apply_fn(config, ledger, runner)
        elif action == 'remove':
            require(peer, 'GATEWAY_PEER_UNKNOWN')
            peer['removed'] = True
            save(ledger_path, json.dumps(ledger, sort_keys=True))
            apply_fn(config, ledger, runner)
        else:
            require(peer and not peer['removed'], 'GATEWAY_PEER_UNKNOWN')
        reachable = False
        if not peer['removed']:
            for row in runner(['wg', 'show', config['interface'], 'latest-handshakes']).splitlines():
                fields = row.split()
                if len(fields) == 2 and fields[0] == public_key:
                    reachable = 0 < int(fields[1]) <= now() and now() - int(fields[1]) <= 180
        public = key(runner(['wg', 'pubkey'], input=private(config['private_key_file']).strip() + '\n'))
        return {'status': 'succeeded', 'reachable': reachable, 'tunnel': {
            'server_public_key': public, 'endpoint': config['endpoint'], 'address': peer['address'],
            'allowed_ips': str(server_ip) + '/32'}}
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', help='Private JSON request file; otherwise read stdin')
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, 'GATEWAY_ROOT_REQUIRED')
        config = json.loads(private(args.config))
        if args.request:
            request_path = Path(args.request)
            request_root = Path(config.get('request_root', '/var/lib/railshot/personal/gateway'))
            require(request_path.is_absolute() and request_path.parent == request_root
                    and '..' not in request_path.parts, 'GATEWAY_REQUEST_PATH_INVALID')
            raw = private(request_path, owner=config.get('request_owner_uid', os.geteuid()), max_bytes=16384).encode()
        else:
            raw = sys.stdin.buffer.read(16385)
        require(len(raw) <= 16384)
        result = execute(config, json.loads(raw))
    except Exception:
        print(json.dumps({'status': 'failed', 'error': 'GATEWAY_OPERATION_FAILED'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
