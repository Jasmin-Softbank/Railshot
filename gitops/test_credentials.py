import base64
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import credentials


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.ca = b'synthetic CA fixture'
        self.target = {'secret': 'railshot-k3s-aws', 'target_id': 'k3s-aws',
                       'server': 'https://192.0.2.1:6443', 'project': 'railshot',
                       'namespaces': ['tenant-demo', 'tenant-atlas'],
                       'service_account': {'namespace': 'tenant-demo', 'name': 'railshot-argocd',
                                           'uid': '12345678-1234-1234-1234-123456789012'},
                       'ca_sha256': hashlib.sha256(self.ca).hexdigest(), 'audiences': ['k3s']}
        self.old_token = self.token(self.now - 3600)
        self.new_token = self.token(self.now)
        config = {'bearerToken': self.old_token, 'tlsClientConfig': {
            'caData': base64.b64encode(self.ca).decode(), 'insecure': False}}
        data = {'name': self.target['target_id'], 'server': self.target['server'], 'project': 'railshot',
                'namespaces': ','.join(self.target['namespaces']), 'clusterResources': 'false', 'config': json.dumps(config)}
        self.secret = {'kind': 'Secret', 'metadata': {'name': self.target['secret'], 'namespace': 'argocd',
                        'uid': 'registration-uid', 'resourceVersion': '42', 'labels': credentials.LABELS},
                       'data': {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}
        self.writes = []
        self.calls = []

    def token(self, issued):
        body = {'sub': 'system:serviceaccount:tenant-demo:railshot-argocd', 'iat': issued,
                'exp': issued + 21600, 'aud': ['k3s'], 'kubernetes.io': {
                    'namespace': 'tenant-demo', 'serviceaccount': {
                        'name': 'railshot-argocd', 'uid': self.target['service_account']['uid']}}}
        return 'e30.' + base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=') + '.signature'

    def platform(self, *args, document=None):
        if args[0] == 'patch':
            self.writes.append(copy.deepcopy(document))
            self.assertEqual(document[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '42'})
            self.assertEqual(document[1]['path'], '/data/config')
            self.secret['data']['config'] = document[1]['value']
        return copy.deepcopy(self.secret)

    def customer(self, server, ca, token, path, document=None, *, server_name=None):
        self.assertEqual((server, ca), (self.target['server'], self.ca))
        self.assertEqual(server_name, self.target.get('tls_server_name'))
        self.calls.append(path)
        if path.endswith('/token'):
            self.assertEqual(token, self.old_token)
            self.assertEqual(document['spec'], {'audiences': ['k3s'], 'expirationSeconds': 21600})
            return {'status': {'token': self.new_token, 'expirationTimestamp':
                              credentials.datetime.fromtimestamp(self.now + 21600, credentials.timezone.utc).isoformat()}}
        self.assertEqual(token, self.new_token)
        if path.endswith('/selfsubjectreviews'):
            return {'status': {'userInfo': {'username': 'system:serviceaccount:tenant-demo:railshot-argocd',
                                            'uid': self.target['service_account']['uid']}}}
        return {'kind': 'PodList', 'items': []}

    def test_verified_rotation_preserves_every_other_secret_field_and_scopes_native_cronjob(self):
        before = copy.deepcopy(self.secret)
        with patch('credentials.platform', side_effect=self.platform), patch('credentials.customer', side_effect=self.customer):
            result = credentials.renew(self.target, self.now)
        self.assertEqual(result['status'], 'renewed')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual({k: v for k, v in self.secret['data'].items() if k != 'config'},
                         {k: v for k, v in before['data'].items() if k != 'config'})
        new = json.loads(base64.b64decode(self.secret['data']['config']))
        self.assertEqual(new['bearerToken'], self.new_token)
        self.assertEqual(new['tlsClientConfig'], json.loads(base64.b64decode(before['data']['config']))['tlsClientConfig'])
        self.assertNotIn(self.new_token, json.dumps(result))
        items = credentials.render({'version': 1, 'targets': [self.target]},
                                   'ghcr.io/jasmin-softbank/railshot-api@sha256:' + 'a' * 64)['items']
        role = next(x for x in items if x['kind'] == 'Role')
        self.assertEqual(role['rules'], [{'apiGroups': [''], 'resources': ['secrets'],
                                         'resourceNames': [self.target['secret']], 'verbs': ['get', 'patch']}])
        cron = next(x for x in items if x['kind'] == 'CronJob')['spec']
        self.assertEqual((cron['schedule'], cron['concurrencyPolicy']), ('0 */2 * * *', 'Forbid'))
        self.assertEqual(cron['jobTemplate']['spec']['backoffLimit'], 0)
        pod = cron['jobTemplate']['spec']['template']['spec']
        self.assertEqual(pod['nodeSelector']['railshot.io/node-role'], 'platform')
        self.assertNotIn('hostNetwork', pod)
        self.assertNotIn('hostPath', json.dumps(pod))
        config = json.loads(next(x for x in items if x['kind'] == 'ConfigMap')['data']['kubeconfig'])
        self.assertEqual(config['users'], [{'name': 'railshot-credentials', 'user': {
            'tokenFile': '/var/run/secrets/kubernetes.io/serviceaccount/token'}}])
        self.assertEqual(config['clusters'][0]['cluster'], {'server': 'https://kubernetes.default.svc:443',
            'certificate-authority': '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'})
        self.assertIn({'name': 'KUBECONFIG', 'value': '/etc/railshot/credentials/kubeconfig'},
                      pod['containers'][0]['env'])
        import yaml
        customer_role = list(yaml.safe_load_all(Path(__file__).with_name('credentials-customer.yaml').read_text()))[0]
        self.assertEqual(customer_role['rules'], [{'apiGroups': [''], 'resources': ['serviceaccounts/token'],
                                                 'resourceNames': ['railshot-argocd'], 'verbs': ['create']}])

    def test_explicit_relay_tls_port_preserves_ca_and_six_hour_scoped_renewal(self):
        self.target['server'] = 'https://172.31.0.172:16443'
        self.target['tls_server_name'] = '10.0.0.23'
        self.secret['data']['server'] = base64.b64encode(self.target['server'].encode()).decode()
        config = json.loads(base64.b64decode(self.secret['data']['config']))
        config['tlsClientConfig']['serverName'] = self.target['tls_server_name']
        self.secret['data']['config'] = base64.b64encode(json.dumps(config).encode()).decode()
        credentials.validate_policy({'version': 1, 'targets': [self.target]})
        with patch('credentials.platform', side_effect=self.platform), patch('credentials.customer', side_effect=self.customer):
            self.assertEqual(credentials.renew(self.target, self.now)['status'], 'renewed')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(self.calls), 4)
        for endpoint in ('http://172.31.0.172:16443', 'https://172.31.0.172', 'https://172.31.0.172:0',
                         'https://172.31.0.172:65536', 'https://user@172.31.0.172:16443',
                         'https://172.31.0.172:16443/', 'https://172.31.0.172:16443?x', 'https://172.31.0.172:16443#x'):
            target = {**self.target, 'server': endpoint}
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                credentials.validate_policy({'version': 1, 'targets': [target]})
        for name in ('127.0.0.1', '203.0.113.1', 'relay.test', '::1', None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                credentials.validate_policy({'version': 1, 'targets': [{**self.target, 'tls_server_name': name}]})
        self.writes.clear()
        for name in ('10.0.0.24', None):
            config['tlsClientConfig']['serverName'] = name
            self.secret['data']['config'] = base64.b64encode(json.dumps(config).encode()).decode()
            with patch('credentials.platform', side_effect=self.platform), patch('credentials.customer') as remote, self.assertRaises(ValueError):
                credentials.renew(self.target, self.now)
            remote.assert_not_called()
            self.assertEqual(self.writes, [])

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required for a real local TLS server')
    def test_relay_connects_to_endpoint_but_verifies_registered_name_and_ca_without_proxy_or_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'openssl.cnf'
            config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=extensions\n[dn]\n[extensions]\nsubjectAltName=IP:10.0.0.23\nbasicConstraints=critical,CA:TRUE\n')
            for name in ('server', 'other-ca'):
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                                '-subj', '/CN=fixture', '-config', str(config), '-keyout', str(root / (name + '.key')),
                                '-out', str(root / (name + '.pem'))], check=True, capture_output=True, timeout=15)
            calls = []
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    calls.append(self.path)
                    content = b'{"verified":true}'
                    self.send_response(302 if self.path == '/redirect' else 200)
                    if self.path == '/redirect': self.send_header('Location', '/must-not-follow')
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers(); self.wfile.write(content)
                def log_message(self, *_): pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(root / 'server.pem'), str(root / 'server.key'))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            endpoint = f'https://127.0.0.1:{server.server_port}'
            ca = (root / 'server.pem').read_bytes()
            try:
                with patch.dict(os.environ, {'HTTPS_PROXY': 'http://127.0.0.1:1', 'https_proxy': 'http://127.0.0.1:1', 'NO_PROXY': '', 'no_proxy': ''}):
                    self.assertEqual(credentials.customer(endpoint, ca, 'fixture-token', '/ready', server_name='10.0.0.23'), {'verified': True})
                for name, certificate in (('10.0.0.24', ca), ('10.0.0.23', (root / 'other-ca.pem').read_bytes())):
                    with self.subTest(name=name), self.assertRaises(ssl.SSLCertVerificationError):
                        credentials.customer(endpoint, certificate, 'fixture-token', '/wrong', server_name=name)
                with self.assertRaises(ValueError):
                    credentials.customer(endpoint, ca, 'fixture-token', '/redirect', server_name='10.0.0.23')
                self.assertEqual(calls, ['/ready', '/redirect'])
            finally:
                server.shutdown(); server.server_close(); thread.join(5)

    def test_failed_auth_scope_ca_or_expiry_never_patches_and_never_replays_ambiguous_patch(self):
        original = copy.deepcopy(self.secret)
        for failure in ('ca', 'identity', 'namespace', 'expiry'):
            self.secret = copy.deepcopy(original)
            target = copy.deepcopy(self.target)
            if failure == 'ca': target['ca_sha256'] = 'b' * 64
            def remote(*args, **kwargs):
                result = self.customer(*args, **kwargs)
                if failure == 'identity' and args[3].endswith('selfsubjectreviews'):
                    result['status']['userInfo']['uid'] = 'wrong'
                if failure == 'namespace' and '/pods?' in args[3]:
                    raise RuntimeError('synthetic sensitive response')
                if failure == 'expiry' and args[3].endswith('/token'):
                    result['status']['token'] = self.token(self.now - 21000)
                return result
            with self.subTest(failure=failure), patch('credentials.platform', side_effect=self.platform), \
                    patch('credentials.customer', side_effect=remote), self.assertRaises(Exception):
                credentials.renew(target, self.now)
            self.assertEqual(self.writes, [])
            self.assertEqual(self.secret, original)
        for arrived in (True, False):
            self.secret = copy.deepcopy(original); self.writes = []
            def ambiguous(*args, document=None):
                if args[0] == 'patch':
                    if arrived: self.platform(*args, document=document)
                    else: self.writes.append(copy.deepcopy(document))
                    raise RuntimeError('synthetic timeout')
                return copy.deepcopy(self.secret)
            with patch('credentials.platform', side_effect=ambiguous), patch('credentials.customer', side_effect=self.customer):
                result = credentials.renew(self.target, self.now)
            self.assertEqual(result['status'], 'renewed' if arrived else 'unchanged')
            self.assertEqual(len(self.writes), 1)


if __name__ == '__main__':
    unittest.main()
