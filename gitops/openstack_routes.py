"""Run the fixed Octavia route worker through the registered private SSH path."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

from edge import read_private
from handoff import require
import openstack_route_worker as worker

COMMAND = 'sudo -n /usr/bin/python3 /opt/railshot/octavia/openstack_route_worker.py'
PRIVATE = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


class RouteError(ValueError):
    def __init__(self, *, unknown):
        self.unknown = unknown
        self.code = 'OPENSTACK_ROUTE_OUTCOME_UNKNOWN' if unknown else 'OPENSTACK_ROUTE_BLOCKED'
        super().__init__(self.code)


def ssh_prefix(connection):
    require(isinstance(connection, dict) and set(connection) == {
        'host', 'port', 'user', 'identity_file', 'known_hosts_file', 'host_key_alias'},
        'OPENSTACK_SSH_CONFIGURATION_INVALID')
    for key in ('host', 'host_key_alias'):
        address = ipaddress.IPv4Address(connection[key])
        require(str(address) == connection[key] and any(address in network for network in PRIVATE),
                'OPENSTACK_PRIVATE_SSH_REQUIRED')
    require(isinstance(connection['user'], str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', connection['user'])
            and connection['user'] != 'root' and type(connection['port']) is int
            and 1 <= connection['port'] <= 65535, 'OPENSTACK_SSH_CONFIGURATION_INVALID')
    for key in ('identity_file', 'known_hosts_file'):
        path = Path(connection[key])
        info = path.lstat()
        require(path.is_absolute() and path.resolve() == path and stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'OPENSTACK_PRIVATE_SSH_FILE_REQUIRED')
    return ['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'IdentityAgent=none', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UserKnownHostsFile=' + connection['known_hosts_file'],
            '-o', 'HostKeyAlias=' + connection['host_key_alias'], '-o', 'ConnectTimeout=15',
            '-i', connection['identity_file'], '-p', str(connection['port'])]


def ensure(config_path, request):
    config = read_private(config_path)
    require(isinstance(config, dict) and set(config) == {
        'version', 'provider', 'base_domain', 'runtime_private_address', 'controller', 'proxy'}
        and type(config['version']) is int and config['version'] == 1 and config['provider'] == 'openstack',
        'OPENSTACK_ROUTE_CONFIGURATION_INVALID')
    worker.checked_request(config, request)
    controller, proxy = config['controller'], config['proxy']
    proxy_command = [*ssh_prefix(proxy), '-W', '%h:%p', proxy['user'] + '@' + proxy['host']]
    command = [*ssh_prefix(controller), '-o', 'ProxyCommand=' + shlex.join(proxy_command),
               controller['user'] + '@' + controller['host'], COMMAND]
    try:
        result = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True,
                                shell=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise RouteError(unknown=True) from None
    if len(result.stdout) > 65536:
        raise RouteError(unknown=True)
    try:
        receipt = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RouteError(unknown=True) from None
    if (result.returncode == 1 and isinstance(receipt, dict)
            and set(receipt) == {'status', 'https_verified', 'reason'}
            and receipt['status'] in ('blocked', 'unknown') and receipt['https_verified'] is False
            and isinstance(receipt['reason'], str) and receipt['reason']):
        # Preserve the worker's state without forwarding its diagnostic text.
        raise RouteError(unknown=receipt['status'] == 'unknown')
    if result.returncode != 0:
        raise RouteError(unknown=True)
    if not (isinstance(receipt, dict) and receipt.get('status') == 'configured'
            and receipt.get('https_verified') is False
            and all(receipt.get(key) == value for key, value in request.items())
            and isinstance(receipt.get('resources'), dict)):
        raise RouteError(unknown=True)
    if not (set(receipt['resources']) == {'pool', 'member', 'monitor', 'policy', 'rule'}
            and all(isinstance(value, str) and re.fullmatch(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value)
                    for value in receipt['resources'].values())):
        raise RouteError(unknown=True)
    if not (isinstance(receipt.get('network_rule_id'), str) and re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', receipt['network_rule_id'])):
        raise RouteError(unknown=True)
    return receipt
