import base64
import copy
import hashlib
import json
from pathlib import Path
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

    def customer(self, server, ca, token, path, document=None):
        self.assertEqual((server, ca), (self.target['server'], self.ca))
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
        import yaml
        customer_role = list(yaml.safe_load_all(Path(__file__).with_name('credentials-customer.yaml').read_text()))[0]
        self.assertEqual(customer_role['rules'], [{'apiGroups': [''], 'resources': ['serviceaccounts/token'],
                                                 'resourceNames': ['railshot-argocd'], 'verbs': ['create']}])

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
