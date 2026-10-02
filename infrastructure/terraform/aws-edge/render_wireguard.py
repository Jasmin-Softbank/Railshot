#!/usr/bin/env python3
"""Render one reviewed WireGuard peer using a private key file; never print keys.

Configuration only: no package install, key generation, service start, forwarding
policy or cloud mutation. Apply the reviewed file with native wg-quick separately.
"""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import stat
import tempfile

REPO = Path(__file__).resolve().parents[3]
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def key(value):
    try:
        if not isinstance(value, str) or len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise ValueError('WireGuard key must encode exactly 32 bytes') from exc
    return value


def private_network(value):
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('AllowedIPs must contain canonical private IPv4 networks') from exc
    if not any(network.subnet_of(allowed) for allowed in PRIVATE_NETWORKS):
        raise ValueError('AllowedIPs must stay inside RFC1918 networks; no default or public routes')
    return network


def private_file_path(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() == REPO or REPO in path.resolve().parents:
        raise ValueError('Private files must use absolute non-symlink paths outside the checkout')
    return path


def render(public, private_key):
    if not isinstance(public, dict) or set(public) != {'address', 'peer'}:
        raise ValueError('Public configuration requires only address and peer')
    peer = public['peer']
    if not isinstance(peer, dict) or set(peer) != {'public_key', 'endpoint', 'allowed_ips'}:
        raise ValueError('Peer requires public_key, endpoint and allowed_ips')
    try:
        address = ipaddress.IPv4Interface(public['address'])
        private_network(str(address.network))
        endpoint, port = peer['endpoint'].rsplit(':', 1)
        endpoint = ipaddress.IPv4Address(endpoint)
        if port != '51820' or not endpoint.is_global or endpoint.is_multicast or endpoint.is_reserved:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Use a private interface address and a known public IPv4:51820 endpoint') from exc
    allowed = peer['allowed_ips']
    if (not isinstance(allowed, list) or not 1 <= len(allowed) <= 16
            or not all(isinstance(value, str) for value in allowed) or len(set(allowed)) != len(allowed)):
        raise ValueError('Use 1-16 distinct reviewed AllowedIPs entries')
    networks = [private_network(value) for value in allowed]
    if any(address.ip in network for network in networks):
        raise ValueError('Peer routes must not contain this interface address')
    return ('[Interface]\nAddress = ' + str(address) + '\nListenPort = 51820\nPrivateKey = ' + key(private_key) +
            '\n\n[Peer]\nPublicKey = ' + key(peer['public_key']) + '\nEndpoint = ' + str(endpoint) + ':51820' +
            '\nAllowedIPs = ' + ', '.join(str(n) for n in networks) + '\nPersistentKeepalive = 25\n')


def prepare(public_path, private_key_path, output, *, check=False):
    private_key_path, output = private_file_path(private_key_path), private_file_path(output)
    if private_key_path.resolve() == output.resolve():
        raise ValueError('Output must not overwrite the private key file')
    metadata = private_key_path.stat()
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()):
        raise ValueError('Private key must be an owned regular file with mode 0600')
    public = json.loads(Path(public_path).read_text())
    configuration = render(public, private_key_path.read_text().strip())
    if check:
        return {'configuration_valid': True, 'written': False, 'installed': False}
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = output.parent.stat()
    if stat.S_IMODE(directory.st_mode) != 0o700 or directory.st_uid != os.geteuid():
        raise ValueError('Output directory must be owned and mode 0700')
    if output.exists() and (not output.is_file() or stat.S_IMODE(output.stat().st_mode) != 0o600 or output.stat().st_uid != os.geteuid()):
        raise ValueError('Existing output must be an owned regular file with mode 0600')
    with tempfile.NamedTemporaryFile(mode='w', dir=output.parent, prefix='.wireguard-', delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(configuration)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(temporary_path, output)
        finally:
            temporary_path.unlink(missing_ok=True)
    return {'configuration_valid': True, 'written': True, 'path': str(output), 'installed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-config', required=True, type=Path)
    parser.add_argument('--private-key-file', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        result = prepare(args.public_config, args.private_key_file, args.output, check=args.check)
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc), 'written': False, 'installed': False}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
