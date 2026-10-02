#!/usr/bin/env python3
"""Connect a verified app publication to its registered native edge and authoritative DNS."""
import argparse
import hashlib
import json
import os
from pathlib import Path

import application_release
import applications
import environment as runtime
import dns
import edge


def ensure(config_path, publication_request):
    prepared = application_release.finalize(config_path, publication_request)
    config = applications.load_config(config_path)
    directory = Path(config['state_dir']) / prepared['application_id'] / 'deployments' / publication_request['deployment_id']
    # finalize has checked both hashes against release.json and the current registration.
    request = runtime.read_private(directory / 'route-request.json')
    ingress = request['ingress']
    applications.require(request['provider'] in ('aws', 'gcp'), 'APPLICATION_ROUTE_PROVIDER_UNAVAILABLE')
    applications.require(all(isinstance(ingress.get(key), str) and Path(ingress[key]).is_absolute()
                             for key in ('edge_config_file', 'dns_config_file')), 'APPLICATION_ROUTE_NOT_CONFIGURED')
    dns_config = dns.config_at(ingress['dns_config_file'])
    applications.require(dns_config['base_domain'] == ingress['base_domain'], 'APPLICATION_DNS_ZONE_MISMATCH')
    app_id = prepared['application_id']
    if request['provider'] == 'aws':
        registry = runtime.read_private(config['registry_file'])
        _, descriptor = runtime.registered_node(registry['targets'][request['environment_id']],
                                                request['environment_id'], 'route.' + app_id)
        security_group = runtime.aws_security_group(descriptor, ingress.get('target_security_group_id'))
        allocation = edge.prepare(ingress['edge_config_file'], {
            'target_id': app_id, 'tenant': request['tenant'], 'app': request['app'],
            'environment_id': request['environment_id'], 'provider_kind': 'aws',
            'target_private_ip': request['resource']['private_address'], 'namespace': request['namespace'],
            'health_path': request['health_path'], 'expected_status': 200, 'node_port': request['node_port'],
            'manage_dns': False, 'target_security_group_id': security_group, 'expires_at': ingress.get('expires_at'),
        })
        applications.require(all(allocation[key] == request[key] for key in ('hostname', 'node_port')),
                             'APPLICATION_ROUTE_BINDING_MISMATCH')
        edge.ensure(allocation['reference'])
        native_config, applied = edge.load(allocation['reference'])
        if applied['phase'] != 'applied':
            raise applications.RegistrationError('APPLICATION_ROUTE_RECONCILE_REQUIRED', unknown=applied['phase'] == 'applying')
        outputs = json.loads(edge.terraform(native_config, 'output', '-json'))
        target = outputs['alb_dns_name']['value'].rstrip('.')
        applications.require(target.endswith('.elb.amazonaws.com'), 'APPLICATION_ROUTE_BINDING_MISMATCH')
        dns_request = {'application_id': app_id, 'hostname': request['hostname'], 'type': 'CNAME', 'content': target}
        edge_receipt = {'provider': 'aws', 'plan_sha256': applied['plan_sha256'], 'route_key': applied['route_key']}
    else:
        import gcp_routes
        native = gcp_routes.ensure(ingress['edge_config_file'], {
            'application_id': app_id, 'hostname': request['hostname'], 'node_port': request['node_port'],
            'health_path': request['health_path'],
        })
        applications.require(native['hostname'] == request['hostname'], 'APPLICATION_ROUTE_BINDING_MISMATCH')
        auth = native['dns_authorization_record']
        applications.require(auth['type'] == 'CNAME' and auth['data'].rstrip('.').endswith('.authorize.certificatemanager.goog'),
                             'APPLICATION_CERTIFICATE_DNS_INVALID')
        dns.ensure(ingress['dns_config_file'], {'application_id': app_id, 'purpose': 'certificate',
            'application_hostname': request['hostname'], 'hostname': auth['name'].rstrip('.'),
            'type': 'CNAME', 'content': auth['data'].rstrip('.')})
        dns_request = {'application_id': app_id, 'hostname': request['hostname'], 'type': 'A', 'content': native['frontend_ip']}
        edge_receipt = {'provider': 'gcp', 'backend_service': native['backend_service']}
    proof = dns.ensure(ingress['dns_config_file'], dns_request)
    result = {**prepared, 'phase': 'route_configured', 'public_route_state': 'configured',
              'https_verified': False, 'dns_record_id': proof['record_id'], 'edge': edge_receipt,
              'route_request_sha256': hashlib.sha256(runtime.read_private(directory / 'route-request.json', raw=True)).hexdigest()}
    runtime.save(directory / 'route-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = ensure(args.config, application_release.request_file(args.request))
    except Exception as error:
        # A native operation may have written before returning an error. Never replay it blindly.
        known = isinstance(error, (applications.RegistrationError, dns.DNSError))
        unknown = not known or getattr(error, 'unknown', False) or getattr(error, 'outcome', None) == 'UNKNOWN'
        result = {'status': 'unknown' if unknown else 'blocked', 'public_route_state': 'unverified',
                  'error': {'code': error.code if known else 'APPLICATION_ROUTE_RECONCILE_REQUIRED',
                            'retryable': False, 'outcome_unknown': unknown}}
    print(json.dumps(result))
    return 0 if result['status'] == 'succeeded' else 3


if __name__ == '__main__':
    raise SystemExit(main())
