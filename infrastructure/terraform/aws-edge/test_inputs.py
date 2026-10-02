"""Offline input/routing contracts. No provider, state, credentials or cloud calls."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parent


def fixture():
    return {
        'name': 'railshot-edge', 'account_id': '123456789012', 'region': 'ap-northeast-2',
        'vpc_id': 'vpc-0123456789abcdef0',
        'public_subnet_ids': ['subnet-0123456789abcdef0', 'subnet-0123456789abcdef1'],
        'zone_id': 'ZEXAMPLE', 'base_domain': 'example.com',
        'wireguard_network_interface_id': 'eni-0123456789abcdef0',
        'wireguard_security_group_id': 'sg-0123456789abcdef0',
        'wireguard_peer_cidrs': ['192.0.2.20/32'],
        'wireguard_route_table_ids': ['rtb-0123456789abcdef0', 'rtb-0123456789abcdef1'],
        'routes': {
            'aws-demo-a1b2': {'host': 'aws-demo-a1b2.example.com', 'provider_kind': 'aws',
                'target_private_ip': '172.31.10.20', 'node_port': 30080,
                'health_path': '/healthz', 'priority': 100, 'target_security_group_id': 'sg-0123456789abcdef1'},
            'gcp-demo-c3d4': {'host': 'gcp-demo-c3d4.example.com', 'provider_kind': 'gcp',
                'target_private_ip': '10.66.0.2', 'node_port': 30081, 'health_path': '/ready', 'priority': 200},
        },
    }


def evaluate(values, expression='local.private_routes'):
    with tempfile.TemporaryDirectory(prefix='railshot-edge-inputs-') as directory:
        work = Path(directory)
        (work / 'variables.tf').write_text((MODULE / 'variables.tf').read_text())
        source = (MODULE / 'main.tf').read_text()
        (work / 'locals.tf').write_text('locals {' + source.split('locals {', 1)[1].split('\ndata "aws_subnet"', 1)[0])
        (work / 'fixture.tfvars.json').write_text(json.dumps(values))
        result = subprocess.run(['terraform', 'console', '-no-color', '-var-file=fixture.tfvars.json'],
                                input='jsonencode(' + expression + ')\n', text=True, capture_output=True, cwd=work)
        if result.returncode or 'Error:' in result.stderr:
            raise ValueError(result.stderr)
        return json.loads(json.loads(result.stdout))


class EdgeInputsTest(unittest.TestCase):
    def test_aws_and_gcp_apps_use_private_targets_and_only_gcp_needs_routes(self):
        values = fixture()
        routes = evaluate(values)
        self.assertEqual(len(routes), 2)
        self.assertEqual({r['ip'] for r in routes.values()}, {'10.66.0.2'})
        self.assertEqual({r['table'] for r in routes.values()}, set(values['wireguard_route_table_ids']))
        self.assertEqual(evaluate(values, 'local.gcp_ports'), ['30081'])
        self.assertEqual(evaluate(values, 'var.certificate_arn'), None)
        self.assertEqual(evaluate(values, 'var.http_redirect'), False)

    def test_reused_node_and_duplicate_sg_port_do_not_duplicate_network_rules(self):
        values = fixture()
        alias = copy.deepcopy(values['routes']['aws-demo-a1b2'])
        alias.update(host='aws-alias-e5f6.example.com', priority=300)
        values['routes']['aws-alias-e5f6'] = alias
        self.assertEqual(len(evaluate(values, 'local.aws_target_rules')), 1)
        alias = copy.deepcopy(values['routes']['gcp-demo-c3d4'])
        alias.update(host='gcp-other-f1e2.example.com', priority=400, node_port=30082)
        values['routes']['gcp-other-f1e2'] = alias
        self.assertEqual(len(evaluate(values)), 2)
        self.assertEqual(evaluate(values, 'local.gcp_ports'), ['30081', '30082'])

    def test_aws_only_can_reserve_gateway_eip_without_inventing_a_peer(self):
        values = fixture()
        values['wireguard_peer_cidrs'] = []
        self.assertFalse(evaluate(values, 'local.wireguard_routing_configured'))
        del values['routes']['gcp-demo-c3d4']
        values['wireguard_route_table_ids'] = []
        self.assertTrue(evaluate(values, 'local.wireguard_routing_configured'))
        self.assertEqual(evaluate(values), {})

    def test_zone_can_be_created_before_delegation_or_reused_by_id(self):
        values = fixture()
        self.assertEqual(evaluate(values, 'var.zone_id'), 'ZEXAMPLE')
        del values['zone_id']
        self.assertIsNone(evaluate(values, 'var.zone_id'))
        for invalid in ('', 'example.com', '/hostedzone/ZEXAMPLE'):
            values['zone_id'] = invalid
            with self.subTest(zone_id=invalid), self.assertRaises(ValueError):
                evaluate(values)

    def test_public_metadata_cgnat_ipv6_and_invalid_routes_fail_closed(self):
        changes = [('target_private_ip', ip) for ip in ('8.8.8.8', '169.254.169.254', '100.64.0.1', '::1', '10.999.0.1')]
        changes += [('node_port', 80), ('node_port', 32768), ('priority', 0), ('priority', 1.5),
                    ('health_path', '/ok\ninjected'), ('host', '*.example.com'), ('provider_kind', 'azure')]
        for field, value in changes:
            values = fixture()
            values['routes']['gcp-demo-c3d4'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                evaluate(values)

    def test_host_priority_sg_and_peer_ownership_validation(self):
        for mutation in ('host', 'priority', 'aws-sg', 'gcp-sg', 'peer'):
            values = fixture()
            if mutation == 'host': values['routes']['gcp-demo-c3d4']['host'] = values['routes']['aws-demo-a1b2']['host']
            elif mutation == 'priority': values['routes']['gcp-demo-c3d4']['priority'] = 100
            elif mutation == 'aws-sg': del values['routes']['aws-demo-a1b2']['target_security_group_id']
            elif mutation == 'gcp-sg': values['routes']['gcp-demo-c3d4']['target_security_group_id'] = 'sg-0123456789abcdef1'
            elif mutation == 'peer': values['wireguard_peer_cidrs'] = ['0.0.0.0/0']
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                evaluate(values)


if __name__ == '__main__':
    unittest.main(verbosity=2)
