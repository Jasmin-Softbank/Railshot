#!/usr/bin/env python3
"""Environment-bound custody client; sensitive input/output uses private pipes only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

CONFIG = Path('/etc/railshot/recovery-escrow.json')
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
KINDS = {'vault-initialization': 'initialization-resume', 'vault-delivery-approle': 'delivery-handoff'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect rejected')


def private(path, *, secret=False):
    path = Path(path); info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & (0o077 if secret else 0o022):
        raise ValueError('unsafe escrow file')
    return path.read_text()


class Client:
    def __init__(self, path=CONFIG):
        self.config = config = json.loads(private(path, secret=True))
        if (not isinstance(config, dict) or set(config) != {'version', 'endpoint', 'ca_file', 'client_cert_file', 'client_key_file'}
                or config['version'] != 1):
            raise ValueError('invalid escrow configuration')
        parsed = urlsplit(config['endpoint'])
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path != '/api/v1/escrows'):
            raise ValueError('invalid escrow endpoint')
        for name in ('ca_file', 'client_cert_file', 'client_key_file'):
            if not Path(config[name]).is_absolute(): raise ValueError('absolute escrow references required')
            private(config[name], secret=name == 'client_key_file')
        context = ssl.create_default_context(cafile=config['ca_file'])
        context.load_cert_chain(config['client_cert_file'], config['client_key_file'])
        self.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())

    def request(self, method, suffix='', body=None):
        request = Request(self.config['endpoint'] + suffix, data=None if body is None else canonical(body), method=method,
                          headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=15) as response:
            if response.status not in (200, 201): raise ValueError('escrow rejected')
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024: raise ValueError('escrow response too large')
            return json.loads(raw)

    @staticmethod
    def ack(value):
        if (not isinstance(value, dict) or set(value) != {'stored', 'receipt_id'} or value['stored'] is not True
                or not isinstance(value['receipt_id'], str) or IDENTITY.fullmatch(value['receipt_id']) is None):
            raise ValueError('invalid escrow acknowledgement')
        return value

    def lookup(self, environment_id, kind, operation_id):
        # The mTLS principal fixes environment_id server-side; no caller can select another environment.
        try:
            return self.ack(self.request('GET', '?' + urlencode({'kind': kind, 'operation_id': operation_id})))
        except HTTPError as exc:
            if exc.code == 404: return None
            raise

    def store(self, payload):
        try:
            ack = self.ack(self.request('POST', body=payload))
        except HTTPError as exc:
            if exc.code < 500: raise
            observed = self.lookup(payload['environment_id'], payload['kind'], payload['operation_id'])
            if observed is None: raise ValueError('escrow outcome is unknown')
            return observed
        except (OSError, ValueError):
            # A lost POST response is not a second mutation. Resolve its stable operation first.
            observed = self.lookup(payload['environment_id'], payload['kind'], payload['operation_id'])
            if observed is None: raise ValueError('escrow outcome is unknown')
            return observed
        observed = self.lookup(payload['environment_id'], payload['kind'], payload['operation_id'])
        if observed != ack: raise ValueError('escrow receipt does not match the operation')
        return ack

    def recover(self, environment_id, kind, operation_id):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        ack = self.lookup(environment_id, kind, operation_id)
        if ack is None: return None
        purpose = KINDS[kind]
        cert = x509.load_pem_x509_certificate(private(self.config['client_cert_file']).encode())
        key = serialization.load_pem_private_key(private(self.config['client_key_file'], secret=True).encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048: raise ValueError('RSA recipient required')
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        expected = {'version': 1, 'environment_id': environment_id, 'kind': kind, 'operation_id': operation_id,
                    'receipt_id': ack['receipt_id'], 'purpose': purpose, 'recipient_fingerprint': fingerprint}
        envelope = self.request('POST', '/' + ack['receipt_id'] + '/exports', {'purpose': purpose})
        if (not isinstance(envelope, dict) or set(envelope) != set(expected) | {'wrapped_key', 'nonce', 'ciphertext'}
                or any(envelope[k] != v for k, v in expected.items())):
            raise ValueError('recovery envelope context differs')
        decode = lambda name: base64.b64decode(envelope[name], validate=True)
        dek = key.decrypt(decode('wrapped_key'), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        nonce = decode('nonce')
        if len(dek) != 32 or len(nonce) != 12: raise ValueError('invalid recovery envelope')
        material = json.loads(AESGCM(dek).decrypt(nonce, decode('ciphertext'), canonical(expected)))
        return {**ack, 'material': material}


def run(raw):
    payload = json.loads(raw)
    action = payload.pop('action', 'store') if isinstance(payload, dict) else None
    required = {'version', 'environment_id', 'kind', 'operation_id'} | ({'material'} if action == 'store' else set())
    if (not isinstance(payload, dict) or set(payload) != required or payload.get('version') != 1
            or payload.get('kind') not in KINDS or action not in ('store', 'lookup', 'recover')
            or any(not isinstance(payload[k], str) or IDENTITY.fullmatch(payload[k]) is None for k in ('environment_id', 'operation_id'))):
        raise ValueError('invalid escrow payload')
    client = Client(CONFIG)
    if action == 'store': return client.store(payload)
    result = getattr(client, action)(payload['environment_id'], payload['kind'], payload['operation_id'])
    return {'stored': False} if result is None else result


def main():
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if not raw or len(raw) > 1024 * 1024: raise ValueError('invalid escrow payload size')
        print(json.dumps(run(raw.decode()), separators=(',', ':')))
        return 0
    except Exception:
        print(json.dumps({'stored': False, 'error': 'ESCROW_FAILED'}, separators=(',', ':')))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
