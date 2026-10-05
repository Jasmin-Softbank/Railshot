import importlib.util
from pathlib import Path
import copy
import pytest

spec = importlib.util.spec_from_file_location('slot_config', Path(__file__).parents[1] / 'personal_slot_config.py')
slot = importlib.util.module_from_spec(spec); spec.loader.exec_module(slot)
IDS = ['00000000-0000-4000-8000-' + str(n).zfill(12) for n in range(1, 7)]


def inputs():
    lb, listener, amphora, port, server, subnet = IDS
    return dict(evidence={
        'loadbalancer': {'id': lb, 'name': 'railshot-personal-slot-20261004', 'project_id': 'a'*32,
                        'provider': 'amphora', 'admin_state_up': True, 'provisioning_status': 'ACTIVE',
                        'operating_status': 'ONLINE', 'vip_subnet_id': subnet, 'vip_address': '10.0.0.45'},
        'listener': {'id': listener, 'project_id': 'a'*32, 'loadbalancers': lb, 'admin_state_up': True,
                     'provisioning_status': 'ACTIVE', 'operating_status': 'ONLINE', 'protocol': 'TERMINATED_HTTPS',
                     'protocol_port': 443, 'default_pool_id': None, 'allowed_cidrs': ['10.0.0.0/26']},
        'amphora': {'id': amphora, 'loadbalancer_id': lb, 'status': 'ALLOCATED', 'role': 'STANDALONE',
                    'vrrp_port_id': port, 'compute_id': server, 'vrrp_ip': '10.0.0.46'},
        'port': {'id': port, 'device_id': server, 'project_id': 'a'*32, 'device_owner': 'compute:nova',
                 'network_id': 'network', 'fixed_ips': [{'subnet_id': subnet, 'ip_address': '10.0.0.46'}]},
        'subnet': {'id': subnet, 'network_id': 'network', 'cidr': '10.0.0.0/26'}},
        applications={'version': 1, 'state_dir': '/old/state', 'environments': {
            'k3s-openstack': {'provider': 'openstack', 'target': {'ingress_cidrs': ['10.0.0.18/32']},
                              'ingress': {'base_domain': 'railshot.io', 'edge_config_file': '/old/edge'}}}},
        edge={'version': 1, 'provider': 'openstack', 'base_domain': 'railshot.io',
              'runtime_private_address': '10.0.0.17',
              'controller': {'user': 'legacy', 'identity_file': '/old/key'},
              'proxy': {'identity_file': '/proxy/key'}},
        dns={'version': 1, 'base_domain': 'railshot.io', 'token_file': '/secret/token', 'state_dir': '/old/dns'},
        directory='/config/slot', state_directory='/state/slot', controller_identity_file='/config/slot/key',
        credentials_file='/config/slot/credentials.json', ca_file='/config/slot/ca.pem',
        tunnel_id='3b5be82d-205b-45e0-8910-a23952f4948d')


def test_render_requires_new_slot_and_preserves_input_authorities():
    args = inputs(); original = copy.deepcopy(args)
    docs = slot.render_slot(**args)
    assert args == original
    assert 'runtime_private_address' not in docs['edge.template.json']
    assert docs['edge.template.json']['controller']['identity_file'] == '/config/slot/key'
    assert docs['edge.template.json']['proxy']['identity_file'] == '/proxy/key'
    assert docs['applications.template.json']['environments']['openstack-template']['target']['ingress_cidrs'] == ['10.0.0.46/32']
    assert docs['worker-base.json']['network']['amphora_private_address'] == '10.0.0.46'
    assert docs['tunnel.template.json']['origin_vip'] == '10.0.0.45'


@pytest.mark.parametrize('section,key,value', [
    ('loadbalancer', 'name', 'legacy'), ('loadbalancer', 'provisioning_status', 'PENDING_CREATE'),
    ('listener', 'default_pool_id', IDS[0]), ('listener', 'allowed_cidrs', ['0.0.0.0/0']),
    ('listener', 'loadbalancers', IDS[3]), ('amphora', 'loadbalancer_id', IDS[3]),
    ('port', 'device_id', IDS[3]), ('port', 'fixed_ips', []),
])
def test_rejects_unready_cross_bound_or_overbroad_evidence(section, key, value):
    args = inputs(); args['evidence'][section][key] = value
    with pytest.raises(ValueError):slot.render_slot(**args)


def test_legacy_tunnel_cannot_be_selected():
    args = inputs(); args['tunnel_id'] = IDS[0]
    with pytest.raises(ValueError, match='dedicated_tunnel_required'):slot.render_slot(**args)


def test_accepts_openstackclient_multiline_cidr_representation():
    args = inputs(); args['evidence']['listener']['allowed_cidrs'] = '10.0.0.0/26'
    assert slot.render_slot(**args)['tunnel.template.json']['origin_vip'] == '10.0.0.45'
