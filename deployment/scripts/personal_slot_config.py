"""Render one operator-owned personal slot from verified provider readback.

Pure rendering only: this module never writes credentials, cloud resources, or
production configuration. Callers must collect fresh provider evidence and install
the returned documents privately after their independent network/SSH/TLS checks.
"""
import copy
import ipaddress
from pathlib import PurePosixPath
import uuid


def require(value, reason):
    if not value:
        raise ValueError(reason)


def identity(value):
    require(isinstance(value, str), 'invalid_identity')
    try:
        require(str(uuid.UUID(value)) == value, 'invalid_identity')
    except (ValueError, AttributeError):
        raise ValueError('invalid_identity') from None
    return value


def private_ipv4(value):
    ip = ipaddress.IPv4Address(value)
    require(any(ip in ipaddress.ip_network(c) for c in
                ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')), 'private_address_required')
    return str(ip)


def absolute(value):
    require(isinstance(value, str) and value.startswith('/') and '..' not in PurePosixPath(value).parts,
            'absolute_path_required')
    return value


def render_slot(*, evidence, applications, edge, dns, directory, state_directory,
                controller_identity_file, credentials_file, ca_file, tunnel_id):
    """Evidence includes fresh LB, listener, Amphora, service port and subnet objects."""
    lb, listener, amphora, port, subnet = (evidence[k] for k in
                                           ('loadbalancer', 'listener', 'amphora', 'port', 'subnet'))
    require(lb.get('name') == 'railshot-personal-slot-20261004'
            and lb.get('provider') == 'amphora' and lb.get('admin_state_up') is True
            and lb.get('provisioning_status') == 'ACTIVE' and lb.get('operating_status') == 'ONLINE',
            'slot_loadbalancer_not_ready')
    require(listener.get('admin_state_up') is True and listener.get('provisioning_status') == 'ACTIVE'
            and listener.get('operating_status') == 'ONLINE'
            and listener.get('protocol') == 'TERMINATED_HTTPS' and listener.get('protocol_port') == 443
            and listener.get('default_pool_id') is None
            and listener.get('project_id') == lb.get('project_id'), 'slot_listener_not_ready')
    relation = listener.get('loadbalancers')
    relations = relation.splitlines() if isinstance(relation, str) else [x.get('id') for x in relation or []]
    require(relations == [lb['id']], 'slot_listener_mismatch')
    require(amphora.get('loadbalancer_id') == lb['id'] and amphora.get('status') == 'ALLOCATED'
            and amphora.get('role') == 'STANDALONE', 'slot_amphora_not_ready')
    require(port.get('id') == amphora.get('vrrp_port_id') and port.get('device_id') == amphora.get('compute_id')
            and port.get('project_id') == lb.get('project_id') and port.get('device_owner') == 'compute:nova'
            and port.get('network_id') == subnet.get('network_id'), 'slot_port_mismatch')
    cidrs = listener.get('allowed_cidrs')
    cidrs = cidrs.splitlines() if isinstance(cidrs, str) else cidrs
    require(subnet.get('id') == lb.get('vip_subnet_id')
            and cidrs == [subnet.get('cidr')], 'slot_subnet_mismatch')
    address = private_ipv4(amphora['vrrp_ip'])
    require(any(x.get('subnet_id') == subnet['id'] and x.get('ip_address') == address
                for x in port.get('fixed_ips', [])), 'slot_port_address_mismatch')
    require(ipaddress.ip_address(address) in ipaddress.ip_network(subnet['cidr'])
            and ipaddress.ip_address(private_ipv4(lb['vip_address'])) in ipaddress.ip_network(subnet['cidr']),
            'slot_address_outside_subnet')
    directory, state_directory = absolute(directory), absolute(state_directory)
    require(directory != state_directory, 'separate_state_directory_required')
    for p in (controller_identity_file, credentials_file, ca_file): absolute(p)
    for value in (lb['id'], listener['id'], subnet['id'], port['id'], amphora['compute_id'], tunnel_id): identity(value)
    require(tunnel_id == '3b5be82d-205b-45e0-8910-a23952f4948d', 'dedicated_tunnel_required')
    domain = edge['base_domain']
    require(domain == dns['base_domain'] == 'railshot.io', 'domain_mismatch')
    base = {'version': 1, 'project_id': lb['project_id'], 'loadbalancer_id': lb['id'],
            'listener_id': listener['id'], 'member_subnet_id': subnet['id'], 'base_domain': domain,
            'network': {'amphora_port_id': port['id'], 'amphora_server_id': amphora['compute_id'],
                        'amphora_private_address': address}}
    template = copy.deepcopy(applications)
    profile = copy.deepcopy(template['environments']['k3s-openstack'])
    require(profile['provider'] == 'openstack', 'openstack_template_required')
    profile['target']['ingress_cidrs'] = [address + '/32']
    profile['ingress'] = {'base_domain': domain}
    template['environments'] = {'openstack-template': profile}
    template['state_dir'] = state_directory + '/template-applications'
    new_edge = {k: copy.deepcopy(edge[k]) for k in ('version', 'provider', 'base_domain', 'controller', 'proxy')}
    new_edge['controller'].update(user='railshot-personal-slot', identity_file=controller_identity_file)
    new_dns = copy.deepcopy(dns);new_dns['state_dir'] = state_directory + '/dns'
    tunnel = {'version': 1, 'base_domain': domain, 'name': 'railshot-tunnel', 'tunnel_id': tunnel_id,
              'credentials_file': credentials_file, 'origin_vip': lb['vip_address'], 'ca_file': ca_file}
    selector = {'management_network': 'private', 'placement': 'nova',
                'edge_template': directory + '/edge.template.json', 'worker_base_file': directory + '/worker-base.json',
                'tunnel_template': directory + '/tunnel.template.json', 'dns_config_file': directory + '/dns.json'}
    config = {'version': 1, 'application_template': directory + '/applications.template.json',
              'template_environment': 'openstack-template', 'state_dir': state_directory + '/runtimes',
              'route_profiles': {'slot-20261004': selector}}
    return {'applications.template.json': template, 'edge.template.json': new_edge,
            'worker-base.json': base, 'tunnel.template.json': tunnel, 'dns.json': new_dns,
            'personal-runtime.json': config}
