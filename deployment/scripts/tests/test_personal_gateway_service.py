import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from contextlib import nullcontext
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('personal_gateway_service', SCRIPTS / 'personal_gateway_service.py')
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


def request(path, raw):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(path)
        client.sendall(raw)
        response = b''
        while True:
            part = client.recv(4096)
            if not part:
                return response
            response += part


def response(path, method, route, token, body=b''):
    raw = (f'{method} {route} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}\r\n'
           f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body
    reply = request(path, raw)
    head, content = reply.split(b'\r\n\r\n', 1)
    return int(head.split()[1]), json.loads(content)


def test_token_accepts_contract_boundaries_and_rejects_permissions():
    with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as temporary:
        path = Path(temporary) / 'token'
        for token in ('a' * 32, '._~-' * 128):
            path.write_text(token + '\n')
            path.chmod(0o600)
            assert service.private_token(path) == token
        path.write_text('a' * 31)
        try:
            service.private_token(path)
        except ValueError:
            pass
        else:
            raise AssertionError('short token accepted')
        path.write_text('a' * 32)
        path.chmod(0o604)
        try:
            service.private_token(path)
        except ValueError:
            pass
        else:
            raise AssertionError('public token file accepted')


def test_authenticated_socket_forwards_only_bounded_requests_and_health_is_side_effect_free():
    token = 'token._~-value-' + 'a' * 32
    config = {'version': 1}
    with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as temporary:
        path = str(Path(temporary) / 'gateway.sock')
        server = service.UnixHTTPServer(path, service.Handler, config=config, token=token, client_uid=os.getuid())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        result = {'status': 'succeeded', 'reachable': False, 'tunnel': {'address': '10.253.240.2/32'}}
        body = json.dumps({'action': 'register', 'target_id': 'target_1', 'generation': 1,
                           'public_key': 'A' * 43 + '='}).encode()
        try:
            with patch.object(service, 'peer_uid', return_value=os.getuid()), \
                    patch.object(service.gateway, 'execute', return_value=result) as execute, \
                    patch.object(service.gateway, 'validate'), \
                    patch.object(service.gateway, 'locked_ledger', return_value=nullcontext({})) as locked, \
                    patch.object(service.gateway, 'verify_applied') as applied:
                assert response(path, 'POST', '/requests', 'wrong-' + token, body)[0] == 401
                status, value = response(path, 'POST', '/requests', token, body)
                assert status == 200 and value == result
                execute.assert_called_once_with(config, json.loads(body))
                status, value = response(path, 'GET', '/healthz', token)
                assert status == 200 and value == {'status': 'ready'}
                locked.assert_called_once_with(config)
                applied.assert_called_once_with(config, {}, service.gateway.run)
                execute.assert_called_once()
                applied.side_effect = ValueError('GATEWAY_INTERFACE_MISSING')
                status, value = response(path, 'GET', '/healthz', token)
                assert status == 503 and value['error'] == 'GATEWAY_NOT_READY'
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


def test_initialize_normalizes_new_fsgroup_pvc_before_writing_private_state():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        state, wireguard = root / 'state', root / 'wireguard'
        for directory in (state, wireguard):
            directory.mkdir()
            directory.chmod(0o2770)
            service.normalize_private_directory(directory, os.geteuid())
            assert (directory.stat().st_mode & 0o7777) == 0o700
        if os.geteuid() != 0:
            return
        key = root / 'private-key'
        key.write_text('A' * 43 + '=\n')
        key.chmod(0o440)
        with patch.object(service, 'STATE_DIRECTORY', state), \
                patch.object(service, 'WIREGUARD_DIRECTORY', wireguard), \
                patch.object(service.gateway, 'key'), patch.object(service.gateway, 'validate'):
            config = service.initialize(state / 'config.json', key, 'gateway.example:51820')
        assert config['private_key_file'] == str(state / 'private.key')
        assert (state / 'private.key').stat().st_mode & 0o777 == 0o600
