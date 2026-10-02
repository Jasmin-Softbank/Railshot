"""Real publication preparation with native edge and authoritative DNS boundaries stubbed."""
from contextlib import contextmanager, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import application_routes as routes
import applications
import environment as runtime
import gcp_routes
import test_application_release as release_fixtures
import test_applications as registration_fixtures


class ApplicationRoutesTest(unittest.TestCase):
    @contextmanager
    def fixture(self, provider='aws', ingress_patch=None):
        original = registration_fixtures.ApplicationsTest.setUp

        def setup_registration(case):
            original(case)
            profile = case.config['environments'][case.env_id]
            profile['provider'] = provider
            profile['ingress'].update(edge_config_file=str(case.root / 'edge.json'),
                dns_config_file=str(case.root / 'dns.json'), expires_at='2099-01-01T00:00:00Z')
            profile['ingress'].update(ingress_patch or {})
            case.fixture.write('dns.json', {'version': 1, 'base_domain': 'railshot.io', 'zone_id': 'a' * 32,
                'token_file': str(case.root / 'unused-token'), 'state_dir': str(case.root / 'dns-state')})
            if provider == 'gcp':
                descriptor = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text())
                descriptor['target_id'] = case.env_id
                case.fixture.write('descriptor.json', descriptor)
            elif provider == 'openstack':
                case.fixture.use_openstack()
                case.fixture.write('registry.json', case.fixture.registry)
            case.write_config()

        case = release_fixtures.ApplicationReleaseTest(methodName='runTest')
        try:
            with patch.object(registration_fixtures.ApplicationsTest, 'setUp', setup_registration):
                case.setUp()
            case.edge.side_effect = None
            case.edge.return_value = {'reference': '/private/native/route.json',
                'hostname': case.registered['hostname'], 'node_port': case.registered['node_port']}
            case.native_ensure = case.enterContext(patch.object(routes.edge, 'ensure'))
            case.native_load = case.enterContext(patch.object(routes.edge, 'load', return_value=(
                {'config': 'native-edge'}, {'phase': 'applied', 'plan_sha256': 'd' * 64, 'route_key': 'allocated-route'})))
            case.terraform = case.enterContext(patch.object(routes.edge, 'terraform', return_value=json.dumps(
                {'alb_dns_name': {'value': 'railshot-123.ap-northeast-2.elb.amazonaws.com.'}})))
            case.registration.native.side_effect = None
            case.registration.native.return_value = json.dumps({'Reservations': [{'Instances': [{
                'InstanceId': 'i-0123456789abcdef0',
                'PrivateIpAddress': case.registration.fixture.descriptor['addresses']['private'],
                'SecurityGroups': [{'GroupId': 'sg-12345678'}],
            }]}]})
            case.gcp_result = {'hostname': case.registered['hostname'], 'frontend_ip': '34.100.10.20',
                'backend_service': 'railshot-calculator', 'dns_authorization_record': {
                    'name': '_acme-challenge_123.' + case.registered['hostname'] + '.', 'type': 'CNAME',
                    'data': 'token.project.authorize.certificatemanager.goog.'}}
            case.gcp = case.enterContext(patch.object(gcp_routes, 'ensure', return_value=case.gcp_result))

            def dns_boundary(config_path, request):
                # Keep the real hostname/zone/certificate ownership validator inside the boundary.
                routes.dns.request_at(routes.dns.config_at(config_path), request)
                return {'record_id': 'f' * 32}

            case.dns = case.enterContext(patch.object(routes.dns, 'ensure', side_effect=dns_boundary))
            yield case
        finally:
            case.doCleanups()

    def assert_configured_only(self, case, result):
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['phase'], 'route_configured')
        self.assertEqual(result['public_route_state'], 'configured')
        self.assertIs(result['https_verified'], False)
        self.assertEqual(result['application_id'], case.registered['application_id'])
        self.assertEqual(result['target_id'], case.registered['target_id'])
        self.assertEqual(result['environment_id'], case.registered['environment_id'])
        self.assertFalse({'url', 'deployed', 'public_http'} & result.keys())
        saved = case.prepared_dir / 'route-result.json'
        self.assertEqual(json.loads(saved.read_bytes()), result)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['route_request_sha256'], hashlib.sha256(
            (case.prepared_dir / 'route-request.json').read_bytes()).hexdigest())
        case.probe.assert_not_called()

    def test_aws_publication_binds_allocated_route_and_dns_without_claiming_https(self):
        with self.fixture() as case:
            result = routes.ensure(case.config_path, case.request)
            self.assert_configured_only(case, result)
            ingress = case.registration.config['environments'][case.registered['environment_id']]['ingress']
            case.edge.assert_called_once_with(ingress['edge_config_file'], {
                'target_id': case.registered['application_id'], 'tenant': 'demo', 'app': case.registered['app'],
                'environment_id': case.registered['environment_id'], 'provider_kind': 'aws',
                'target_private_ip': case.registration.fixture.descriptor['addresses']['private'],
                'namespace': case.registered['namespace'], 'health_path': '/alive', 'expected_status': 200,
                'node_port': case.registered['node_port'], 'manage_dns': False,
                'target_security_group_id': 'sg-12345678', 'expires_at': ingress['expires_at'],
            })
            case.native_ensure.assert_called_once_with('/private/native/route.json')
            case.terraform.assert_called_once_with({'config': 'native-edge'}, 'output', '-json')
            case.dns.assert_called_once_with(ingress['dns_config_file'], {
                'application_id': case.registered['application_id'], 'hostname': case.registered['hostname'],
                'type': 'CNAME', 'content': 'railshot-123.ap-northeast-2.elb.amazonaws.com',
            })
            request = runtime.read_private(case.prepared_dir / 'route-request.json')
            self.assertEqual(request['source_commit'], case.request['publication']['source_commit'])
            self.assertEqual((request['port'], request['route'], request['health_path']), (7070, '/application', '/alive'))
            self.assertEqual(request['images'], case.request['publication']['images'])
            self.assertEqual(result['edge'], {'provider': 'aws', 'plan_sha256': 'd' * 64, 'route_key': 'allocated-route'})
            case.gcp.assert_not_called()

    def test_gcp_publication_registers_exact_certificate_cname_then_application_a(self):
        with self.fixture('gcp') as case:
            result = routes.ensure(case.config_path, case.request)
            self.assert_configured_only(case, result)
            ingress = case.registration.config['environments'][case.registered['environment_id']]['ingress']
            case.gcp.assert_called_once_with(ingress['edge_config_file'], {
                'application_id': case.registered['application_id'], 'hostname': case.registered['hostname'],
                'node_port': case.registered['node_port'], 'health_path': '/alive',
            })
            self.assertEqual([call.args for call in case.dns.call_args_list], [
                (ingress['dns_config_file'], {'application_id': case.registered['application_id'], 'purpose': 'certificate',
                    'application_hostname': case.registered['hostname'], 'hostname': '_acme-challenge_123.' + case.registered['hostname'],
                    'type': 'CNAME', 'content': 'token.project.authorize.certificatemanager.goog'}),
                (ingress['dns_config_file'], {'application_id': case.registered['application_id'],
                    'hostname': case.registered['hostname'], 'type': 'A', 'content': '34.100.10.20'}),
            ])
            case.edge.assert_not_called(); case.registration.native.assert_not_called()

    def test_foreign_certificate_suffix_or_challenge_hostname_never_registers_app_a(self):
        for field, value, error in (
                ('data', 'foreign.example.com.', 'APPLICATION_CERTIFICATE_DNS_INVALID'),
                ('data', 'token.authorize.certificatemanager.goog.attacker.com.', 'APPLICATION_CERTIFICATE_DNS_INVALID'),
                ('name', '_acme-challenge_123.other-app.railshot.io.', 'DNS_CERTIFICATE_HOSTNAME_INVALID')):
            with self.subTest(field=field, value=value), self.fixture('gcp') as case:
                case.gcp_result['dns_authorization_record'][field] = value
                with self.assertRaisesRegex((applications.RegistrationError, routes.dns.DNSError), error):
                    routes.ensure(case.config_path, case.request)
                self.assertFalse(any(call.args[1]['type'] == 'A' for call in case.dns.call_args_list))
                self.assertFalse((case.prepared_dir / 'route-result.json').exists())

    def test_native_route_identity_or_alb_suffix_mismatch_never_writes_dns(self):
        for provider, field in (('aws', 'hostname'), ('aws', 'node_port'), ('gcp', 'hostname'), ('aws', 'alb')):
            with self.subTest(provider=provider, field=field), self.fixture(provider) as case:
                if field == 'alb':
                    case.terraform.return_value = json.dumps({'alb_dns_name': {'value': 'foreign.example.com'}})
                elif provider == 'aws':
                    case.edge.return_value[field] = 'foreign.railshot.io' if field == 'hostname' else 12345
                else:
                    case.gcp_result['hostname'] = 'foreign.railshot.io'
                with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_ROUTE_BINDING_MISMATCH'):
                    routes.ensure(case.config_path, case.request)
                case.dns.assert_not_called()
                if provider == 'aws' and field != 'alb': case.native_ensure.assert_not_called()

    def test_changed_publication_or_prepared_request_is_rejected_before_native_calls(self):
        with self.fixture() as case:
            case.prepare()
            changed = copy.deepcopy(case.request)
            changed['publication']['source_commit'] = 'f' * 40
            with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_RELEASE_CONFLICT'):
                routes.ensure(case.config_path, changed)
            request = runtime.read_private(case.prepared_dir / 'route-request.json')
            runtime.save(case.prepared_dir / 'route-request.json', {**request, 'hostname': 'foreign.railshot.io'})
            with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_RELEASE_CHANGED'):
                routes.ensure(case.config_path, case.request)
            case.edge.assert_not_called(); case.gcp.assert_not_called(); case.dns.assert_not_called()

    def test_missing_native_configuration_and_unsupported_openstack_stop_before_mutation(self):
        for provider, ingress, error in (
                ('aws', {'edge_config_file': None}, 'APPLICATION_ROUTE_NOT_CONFIGURED'),
                ('gcp', {'dns_config_file': 'relative.json'}, 'APPLICATION_ROUTE_NOT_CONFIGURED'),
                ('openstack', {}, 'APPLICATION_ROUTE_PROVIDER_UNAVAILABLE')):
            with self.subTest(provider=provider), self.fixture(provider, ingress) as case:
                with self.assertRaisesRegex(applications.RegistrationError, error):
                    routes.ensure(case.config_path, case.request)
                case.edge.assert_not_called(); case.gcp.assert_not_called(); case.dns.assert_not_called()
                case.registration.native.assert_not_called()

    def test_dns_zone_must_match_registered_application_domain(self):
        with self.fixture() as case:
            config_path = case.registration.root / 'dns.json'
            config = runtime.read_private(config_path); config['base_domain'] = 'foreign.example'
            runtime.save(config_path, config)
            with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_DNS_ZONE_MISMATCH'):
                routes.ensure(case.config_path, case.request)
            case.edge.assert_not_called(); case.dns.assert_not_called()

    def test_cli_keeps_started_native_work_unknown_and_preflight_failure_blocked(self):
        for failure, status, code in (
                ('applying', 'unknown', 'APPLICATION_ROUTE_RECONCILE_REQUIRED'),
                ('native-error', 'unknown', 'APPLICATION_ROUTE_RECONCILE_REQUIRED'),
                ('dns-unknown', 'unknown', 'DNS_API_FAILED'),
                ('config', 'blocked', 'APPLICATION_ROUTE_NOT_CONFIGURED')):
            ingress = {'edge_config_file': None} if failure == 'config' else None
            with self.subTest(failure=failure), self.fixture(ingress_patch=ingress) as case:
                if failure == 'applying':
                    case.native_load.return_value[1]['phase'] = 'applying'
                elif failure == 'native-error':
                    case.native_ensure.side_effect = OSError('private-provider-response')
                elif failure == 'dns-unknown':
                    case.dns.side_effect = routes.dns.DNSError('DNS_API_FAILED', 'UNKNOWN')
                path = case.registration.root / 'publication.json'
                runtime.save(path, case.request)
                output = io.StringIO()
                with patch.object(sys, 'argv', ['application_routes', '--config', str(case.config_path), '--request', str(path)]), \
                        patch.object(routes.os, 'umask'), redirect_stdout(output):
                    self.assertEqual(routes.main(), 3)
                result = json.loads(output.getvalue())
                self.assertEqual(result, {'status': status, 'public_route_state': 'unverified',
                    'error': {'code': code, 'retryable': False, 'outcome_unknown': status == 'unknown'}})
                self.assertNotIn('private-provider-response', output.getvalue())
                self.assertFalse((case.prepared_dir / 'route-result.json').exists())
                if failure != 'dns-unknown': case.dns.assert_not_called()


if __name__ == '__main__':
    unittest.main()
