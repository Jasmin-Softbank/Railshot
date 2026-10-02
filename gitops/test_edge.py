import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import edge


class EdgeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.values = {'base_domain': 'railshot.io', 'region': 'ap-northeast-2', 'routes': {
            name: {'host': name + '.railshot.io', 'node_port': 30080 + i, 'priority': 100 + i,
                   'provider_kind': 'aws', 'target_private_ip': '10.0.0.2', 'health_path': '/health',
                   'target_security_group_id': 'sg-0123456789abcdef0'}
            for i, name in enumerate(('fixture-aws', 'fixture-gcp', 'atlas-aws', 'apex'))}}
        self.values['routes']['apex']['host'] = 'railshot.io'
        edge.durable_write(self.root / 'vars.json', edge.encoded(self.values))
        self.config = {'version': 1, 'state_dir': str(self.root / 'state'), 'terraform_dir': str(self.root),
                       'variables_file': str(self.root / 'vars.json'), 'auto_apply': False}
        self.path = self.root / 'config.json'
        edge.durable_write(self.path, edge.encoded(self.config))
        self.request = {'target_id': 'new-runtime', 'tenant': 'team', 'app': 'new-app',
                        'environment_id': '1234-env', 'provider_kind': 'aws', 'target_private_ip': '10.0.0.3',
                        'target_security_group_id': 'sg-0123456789abcdef0', 'namespace': 'tenant-team',
                        'health_path': '/health', 'expected_json': {'ok': True}, 'expires_at': '2100-01-01T00:00:00Z'}

    def prepared(self):
        return edge.prepare(self.path, self.request)

    def test_stable_collision_safe_allocations_and_owned_recovery(self):
        first = self.prepared()
        self.assertEqual(first, self.prepared())
        # Missing reference file after a crash can be recreated from the committed reservation.
        Path(first['reference']['allocation_path']).unlink()
        self.assertEqual(first, self.prepared())
        with patch('edge.digest', return_value='a' * 64):
            # Force numeric seed collision against an occupied NodePort/priority.
            taken_port = edge.free_number(int('a' * 64, 16), 30000, 32767, set())
            taken_priority = edge.free_number(int('a' * 64, 16), 1000, 49999, set())
            self.values['routes']['apex'].update(node_port=taken_port, priority=taken_priority)
            edge.durable_write(self.root / 'vars.json', edge.encoded(self.values))
            other = edge.prepare(self.path, {**self.request, 'app': 'second-app'})
        self.assertNotEqual(other['node_port'], taken_port)
        self.assertNotEqual(other['priority'], taken_priority)
        self.assertNotEqual(first['hostname'], other['hostname'])
        numeric = edge.prepare(self.path, {**self.request, 'tenant': '123', 'namespace': 'tenant-numeric'})
        self.assertNotEqual(first['hostname'], numeric['hostname'])
        self.assertNotIn(first['node_port'], [r['node_port'] for r in self.values['routes'].values()])
        with self.assertRaisesRegex(ValueError, 'belongs to another'):
            edge.prepare(self.path, {**self.request, 'target_private_ip': '10.0.0.9'})
        with self.assertRaisesRegex(ValueError, 'namespace already allocated'):
            edge.prepare(self.path, {**self.request, 'environment_id': 'other-env'})

    def test_untrusted_endpoint_and_contract_rejected_before_native_effects(self):
        with patch('edge.native') as native:
            for mutation in ({'target_private_ip': '169.254.169.254'}, {'target_private_ip': '8.8.8.8'},
                             {'target_private_ip': '100.64.0.2'}, {'target_private_ip': '::1'},
                             {'namespace': 'kube-system'}, {'health_path': '//evil'}, {'health_path': '/..'},
                             {'provider_kind': 'gcp'}, {'hostname': 'external.example'},
                             {'expires_at': '2000-01-01T00:00:00Z'}):
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    edge.prepare(self.path, {**self.request, **mutation})
            native.assert_not_called()

    def test_saved_plan_preserves_all_routes_and_never_repeats_uncertain_apply(self):
        first = self.prepared(); reference = first['reference']
        key = first['route_key']
        plan = {'resource_changes': [{'address': f'aws_lb_target_group.app["{key}"]',
                                     'change': {'actions': ['create']}}]}
        def terraform(config, *args):
            if args[0] == 'plan':
                variables = json.loads(Path(next(a.split('=', 1)[1] for a in args if a.startswith('-var-file='))).read_text())
                self.assertEqual({k: variables['routes'][k] for k in self.values['routes']}, self.values['routes'])
                Path(next(a.split('=', 1)[1] for a in args if a.startswith('-out='))).write_bytes(b'saved-plan')
                return ''
            if args[0] == 'show':
                return json.dumps(plan)
            raise RuntimeError('synthetic apply interruption')
        with patch('edge.terraform', side_effect=terraform):
            planned = edge.plan_route(reference)
            self.assertEqual(edge.plan_route(reference)['plan_sha256'], planned['plan_sha256'])
            self.assertEqual(planned['plan_sha256'], hashlib.sha256(b'saved-plan').hexdigest())
            with self.assertRaisesRegex(ValueError, 'reviewed'):
                edge.apply_route(reference, 'f' * 64)
            with self.assertRaises(RuntimeError):
                edge.apply_route(reference, planned['plan_sha256'])
            with self.assertRaisesRegex(ValueError, 'reviewed'):
                edge.apply_route(reference, planned['plan_sha256'])
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                edge.plan_route(reference)
        self.assertEqual(edge.load(reference)[1]['phase'], 'applying')
        with patch('edge.terraform') as tf:
            edge.ensure(reference)
            tf.assert_not_called()
        second = edge.prepare(self.path, {**self.request, 'app': 'second-app'})
        config, other = edge.load(second['reference'])
        other.update(phase='planned', plan_sha256='f' * 64); edge.save(config, other)
        with self.assertRaisesRegex(ValueError, 'another edge apply'):
            edge.apply_route(second['reference'], 'f' * 64)

    def test_plan_rejects_existing_route_mutation_replacement_and_sg_broadening(self):
        first = self.prepared()
        for address, actions in [('aws_lb_listener_rule.app["apex"]', ['update']),
                                 ('aws_lb_target_group.app["' + first['route_key'] + '"]', ['delete', 'create']),
                                 ('aws_lb.app', ['update'])]:
            with self.assertRaises(ValueError):
                edge.validate_plan({'resource_changes': [{'address': address, 'change': {'actions': actions}}]}, first)
        old = {'egress': [], 'ingress': [{'from_port': 443}], 'id': 'sg-owned'}
        rule = {'cidr_blocks': [first['route']['target_private_ip'] + '/32'], 'protocol': 'tcp',
                'from_port': first['node_port'], 'to_port': first['node_port']}
        plan = {'resource_changes': [{'address': 'aws_security_group.alb', 'change': {
            'actions': ['update'], 'before': old, 'after': {**old, 'egress': [rule]}}}]}
        self.assertEqual(len(edge.validate_plan(plan, first)), 1)
        rule['cidr_blocks'] = ['0.0.0.0/0']
        with self.assertRaises(ValueError):
            edge.validate_plan(plan, first)
        refresh = {'resource_drift': [{'address': 'aws_lb_target_group.app["apex"]'}],
                   'resource_changes': [{'address': 'aws_lb_target_group.app["apex"]', 'change': {'actions': ['no-op']}}]}
        self.assertEqual(edge.validate_plan(refresh, first), [])
        refresh['resource_changes'][0]['change']['actions'] = ['update']
        with self.assertRaises(ValueError):
            edge.validate_plan(refresh, first)

    def test_gcp_routes_use_same_identity_contract_and_only_exact_private_route_additions(self):
        request = {**self.request, 'provider_kind': 'gcp', 'target_private_ip': '10.66.0.3'}
        del request['target_security_group_id']
        row = edge.prepare(self.path, request)
        self.assertNotIn('target_security_group_id', row['route'])
        plan = {'resource_changes': [{'address': 'aws_route.gcp["rtb-123:10.66.0.3"]', 'change': {
            'actions': ['create'], 'after': {'destination_cidr_block': '10.66.0.3/32'}}}]}
        self.assertEqual(len(edge.validate_plan(plan, row)), 1)
        plan['resource_changes'][0]['change']['after']['destination_cidr_block'] = '10.66.0.0/24'
        with self.assertRaises(ValueError):
            edge.validate_plan(plan, row)

    def test_readback_requires_exact_target_dns_tls_health_and_site_before_url(self):
        row = self.prepared(); reference = row['reference']; route = row['route']
        config, row = edge.load(reference); row['phase'] = 'applying'; edge.save(config, row)
        suffix = '.app[' + json.dumps(row['route_key']) + ']'
        state = {'values': {'root_module': {'resources': [
            {'address': 'aws_lb_target_group' + suffix, 'values': {'arn': 'group'}},
            {'address': 'aws_lb_listener_rule' + suffix, 'values': {'arn': 'rule'}}]},
            'outputs': {'zone_id': {'value': 'Z123'}, 'alb_dns_name': {'value': 'owned.elb.amazonaws.com'}}}}
        health = {'TargetHealthDescriptions': [{'Target': {'Id': route['target_private_ip'], 'Port': route['node_port']},
                                                'TargetHealth': {'State': 'healthy'}}]}
        responses = {'describe-target-groups': {'TargetGroups': [{'TargetType': 'ip', 'Port': route['node_port'],
                         'Protocol': 'HTTP', 'HealthCheckPath': '/health'}]},
                     'describe-rules': {'Rules': [{'Priority': str(route['priority']), 'Conditions': [
                         {'Field': 'host-header', 'HostHeaderConfig': {'Values': [route['host']]}}],
                         'Actions': [{'Type': 'forward', 'TargetGroupArn': 'group'}]}]},
                     'list-resource-record-sets': {'ResourceRecordSets': [{'Name': route['host'] + '.', 'Type': 'A',
                         'AliasTarget': {'DNSName': 'dualstack.owned.elb.amazonaws.com.'}}]},
                     'describe-target-health': health}
        request = {'deployment_id': 'dep-1', 'publication': {**self.request, 'source_commit': 'a' * 40}}
        public = lambda *_: {'state': 'succeeded', 'verified_at': '2026-10-02T00:00:00Z', 'url': row['public_http']['url']}
        with patch('edge.terraform', return_value=json.dumps(state)), patch('edge.native', side_effect=lambda args: json.dumps(responses[args[4]])):
            result = edge.observe(reference, request, 'b' * 40, public, lambda _: True)
            self.assertEqual(result['receipt']['deployment_id'], 'dep-1')
            self.assertEqual(result['site_url'], 'https://' + route['host'] + '/')
            self.assertEqual(edge.load(reference)[1]['phase'], 'applied')
            self.assertIsNone(edge.observe(reference, request, 'b' * 40, public, lambda _: False)['url'])
            health['TargetHealthDescriptions'][0]['TargetHealth']['State'] = 'initial'
            self.assertIsNone(edge.observe(reference, request, 'b' * 40, public, lambda _: True)['url'])
            health['TargetHealthDescriptions'][0]['Target']['Id'] = '10.0.0.99'
            with self.assertRaisesRegex(ValueError, 'target differs'):
                edge.observe(reference, request, 'b' * 40, public, lambda _: True)
        registered = {'target': {'id': self.request['target_id'], 'namespace': self.request['namespace'], 'node_port': row['node_port']},
                      'app': self.request['app'], 'tenant': self.request['tenant'], 'public_http': row['public_http']}
        edge.validate_binding(reference, registered)
        registered['public_http'] = {'url': 'https://evil.example/health', 'expected_json': {'ok': True}}
        with self.assertRaisesRegex(ValueError, 'binding differs'):
            edge.validate_binding(reference, registered)


if __name__ == '__main__':
    unittest.main()
