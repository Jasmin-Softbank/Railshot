"""Read-only credential observation, explicit scope and streamed failure evidence."""
import base64
import copy
import json
import unittest
from unittest.mock import patch
import credentials as c
import test_credentials as fixture

class CredentialObservationTest(unittest.TestCase):

    def setUp(self):
        f = self.f = fixture.CredentialsTest(methodName='runTest')
        f.setUp()
        self.now = f.now
        self.anchor = copy.deepcopy(f.target)
        self.observer = {**copy.deepcopy(self.anchor), 'target_id': 'observer-k3s-aws', 'secret': 'railshot-observer-k3s-aws', 'project': '', 'cluster_read': True}
        self.observer['service_account']['name'] = 'railshot-observer'
        self.observer['namespaces'] = ['tenant-demo']
        s = self.secret = copy.deepcopy(f.secret)
        s['metadata']['name'] = self.observer['secret']
        s['metadata']['labels']['argocd.argoproj.io/secret-type'] = 'railshot-observer'
        auth = json.loads(base64.b64decode(s['data']['config']))
        parts = auth['bearerToken'].split('.')
        p = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        p['sub'] = 'system:serviceaccount:tenant-demo:railshot-observer'
        p['kubernetes.io']['serviceaccount']['name'] = 'railshot-observer'
        p['iat'] -= 60
        p['exp'] -= 60
        parts[1] = base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip('=')
        auth['bearerToken'] = '.'.join(parts)
        for (k, v) in {'name': self.observer['target_id'], 'project': '', 'namespaces': '', 'config': json.dumps(auth)}.items():
            s['data'][k] = base64.b64encode(v.encode()).decode()
        self.policy = {'version': 1, 'targets': [self.anchor, self.observer]}

    def native(self, args, **_):
        self.assertIn('get', args)
        self.assertNotIn('patch', args)
        self.assertNotIn('create', args)
        if 'configmap' in args:
            return json.dumps({'data': {'policy.json': json.dumps(self.policy)}})
        return json.dumps(self.secret if self.observer['secret'] in args else self.f.secret)

    def customer(self, server, ca, token, path, document, **kw):
        self.assertEqual(kw['timeout'], 4)
        self.assertTrue(path.endswith('/selfsubjectreviews'))
        part = token.split('.')[1]
        p = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return {'status': {'userInfo': {'username': p['sub'], 'uid': p['kubernetes.io']['serviceaccount']['uid']}}}

    def test_ready_requires_two_live_authentications_and_minimum_expiry(self):
        with patch.object(c, 'native', side_effect=self.native), patch.object(c, 'customer', side_effect=self.customer) as remote:
            r = c.observe_environment('k3s-aws', 'control', now=self.now)
        self.assertEqual(r['state'], 'ready')
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(r['last_success_at'], c.datetime.fromtimestamp(self.now - 3660, c.timezone.utc).isoformat())
        self.assertEqual(r['expires_at'], c.datetime.fromtimestamp(self.now - 3660 + 21600, c.timezone.utc).isoformat())
        self.assertNotIn('bearer', json.dumps(r))
        self.assertNotIn('serviceaccount', json.dumps(r))

    def test_auth_timeout_never_reports_claim_metadata_as_healthy(self):
        with patch.object(c, 'native', side_effect=self.native), patch.object(c, 'customer', side_effect=TimeoutError('private-bearer')):
            r = c.observe_environment('k3s-aws', 'control', now=self.now)
        self.assertEqual((r['state'], r['reason']), ('collection_failed', 'RENEWAL_TIMEOUT'))
        self.assertIsNone(r['expires_at'])
        self.assertIsNone(r['last_success_at'])
        self.assertNotIn('private', json.dumps(r))

    def test_missing_observer_is_no_data(self):
        self.policy['targets'].pop()
        with patch.object(c, 'native', side_effect=self.native), patch.object(c, 'customer') as remote:
            r = c.observe_environment('k3s-aws', 'control', now=self.now)
        self.assertEqual(r['state'], 'no_data')
        remote.assert_not_called()

    def test_scope_preserves_excluded_policy_rows_and_render_command(self):
        self.policy['targets'].append({**copy.deepcopy(self.anchor), 'target_id': 'k3s-openstack', 'secret': 'railshot-k3s-openstack', 'server': 'https://192.0.2.2:6443'})
        before = copy.deepcopy(self.policy)
        self.assertEqual(c.select_policy(self.policy, ['k3s-aws'])['targets'], [self.anchor, self.observer])
        self.assertEqual(self.policy, before)
        items = c.render(self.policy, 'ghcr.io/jasmin-softbank/railshot-api@sha256:' + 'a' * 64, environments=['k3s-aws'])['items']
        command = next((i for i in items if i['kind'] == 'CronJob'))['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['command']
        self.assertEqual(c.command_environments(command), ['k3s-aws'])
        self.assertEqual(json.loads(next((i for i in items if i['kind'] == 'ConfigMap'))['data']['policy.json']), self.policy)
        for v in (['missing'], ['k3s-aws', 'k3s-aws']):
            with self.assertRaises(ValueError):
                c.select_policy(self.policy, v)

    def test_partial_failure_logs_phase_expiry_and_flushes_before_summary(self):
        policy = self.f.policy(2)
        events = []

        def renew(t, *, on_phase):
            on_phase('token_request', expires_at='2026-10-05T13:00:00Z')
            if t['target_id'].endswith('-0'):
                raise c.URLError(TimeoutError('private-bearer-response'))
            return {'secret': t['secret'], 'status': 'renewed', 'expires_at': '2026-10-05T19:00:00Z'}
        with patch.object(c, 'renew', side_effect=renew):
            r = c.renew_policy(policy, report=events.append)
        self.assertEqual(r[0]['code'], 'RENEWAL_TIMEOUT')
        self.assertEqual(r[1]['status'], 'renewed')
        failed = next((e for e in events if e['event'] == 'renewal_finished' and e.get('code')))
        self.assertEqual((failed['phase'], failed['expires_at']), ('token_request', '2026-10-05T13:00:00Z'))
        self.assertGreaterEqual(failed['duration_ms'], 0)
        self.assertNotIn('private-bearer-response', json.dumps(events))
        with patch.object(c.sys, 'argv', ['credentials.py', 'renew', '--policy', '/unused']), patch.object(c.Path, 'stat') as stat, patch.object(c.Path, 'read_bytes', return_value=json.dumps(policy).encode()), patch.object(c, 'renew', side_effect=renew), patch('builtins.print') as out:
            stat.return_value.st_size = 100
            self.assertEqual(c.main(), 1)
        self.assertTrue(all((call.kwargs.get('flush') for call in out.call_args_list)))
        summary = json.loads(out.call_args_list[-1].args[0])
        self.assertEqual((summary['status'], summary['renewed_count'], summary['failed_count']), ('failed', 1, 1))

    def test_relay_http_errors_keep_status_without_reading_private_body(self):
        for status, code in [(401, 'CUSTOMER_AUTH_REJECTED'), (403, 'CUSTOMER_AUTH_REJECTED'),
                             (503, 'CUSTOMER_API_FAILED')]:
            with self.subTest(status=status), patch.object(c.ssl, 'create_default_context'), \
                    patch.object(c, 'RegisteredHTTPSConnection') as factory:
                connection = factory.return_value
                response = connection.getresponse.return_value
                response.status = status
                response.read.side_effect = AssertionError('must not read an error body')
                with self.assertRaises(c.HTTPError) as raised:
                    c.customer('https://192.0.2.1:6443', b'fixture-ca', 'private-bearer', '/test',
                               server_name='10.0.0.1')
                self.assertEqual(c.renewal_error(raised.exception), code)
                self.assertNotIn('private-bearer', str(raised.exception))
                response.read.assert_not_called()
                connection.close.assert_called_once()

    def test_observer_secret_is_never_an_argo_cluster(self):
        labels = {**c.LABELS, 'argocd.argoproj.io/secret-type': 'railshot-observer'}
        self.assertTrue(c.credential_labels_match(labels, 'observer-k3s-aws', '', []))
        self.assertFalse(c.credential_labels_match(c.LABELS, 'observer-k3s-aws', '', []))
        self.assertFalse(c.credential_labels_match(labels, 'k3s-aws', '', []))
        self.assertFalse(c.credential_labels_match(labels, 'observer-k3s-aws', '', ['app']))
if __name__ == '__main__':
    unittest.main()
