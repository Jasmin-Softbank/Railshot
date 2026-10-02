"""JSON boundary only. No provider vocabulary is passed to the engine."""
import ipaddress
import re
from urllib.parse import urlsplit
from models import AdaptedRequest, DeploymentSpec, ExposureSpec, RequestContext, RuntimeSpec, WorkloadSpec


class InputError(ValueError):
    pass


def obj(value, path, allowed, required=()):
    if not isinstance(value, dict):
        raise InputError(f'{path}: expected object')
    extra = value.keys() - set(allowed)
    missing = set(required) - value.keys()
    if extra or missing:
        raise InputError(f'{path}: unknown={sorted(extra)}, missing={sorted(missing)}')
    return value


def string(value, path, pattern=None, maximum=253):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise InputError(f'{path}: expected nonempty string of at most {maximum} characters')
    if pattern and not re.fullmatch(pattern, value):
        raise InputError(f'{path}: invalid format')
    return value


def integer(value, path, low, high):
    if type(value) is not int or not low <= value <= high:
        raise InputError(f'{path}: expected integer {low}..{high}')
    return value


LABEL = r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'
IMAGE = r'[a-z0-9][a-z0-9./:_-]*(?:@sha256:[a-f0-9]{64})?'


def parse_input(data):
    obj(data, 'input', ('schema_version', 'provider', 'environment_id', 'node', 'workload', 'exposure', 'runtime'),
        ('provider', 'environment_id', 'node', 'workload'))
    if data.get('schema_version', '0.1') != '0.1':
        raise InputError('schema_version: only 0.1 is supported (provisional contract)')
    provider = data['provider']
    if provider not in ('aws', 'gcp', 'openstack'):
        raise InputError('provider: expected aws, gcp or openstack')
    environment = string(data['environment_id'], 'environment_id', LABEL, 63)
    node = obj(data['node'], 'node', ('host', 'ssh_user'), ('host',))
    host = string(node['host'], 'node.host', r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?')
    user = node.get('ssh_user')
    if user is not None:
        user = string(user, 'node.ssh_user', r'[a-z_][a-z0-9_-]*', 32)
    work = obj(data['workload'], 'workload', ('image', 'namespace', 'replicas', 'container_port', 'health_path', 'sample_content'),
               ('image', 'namespace'))
    image = string(work['image'], 'workload.image', IMAGE, 512)
    # Require an explicit tag or digest. Reject implicit latest before touching Linux.
    if '@sha256:' not in image and ':' not in image.rsplit('/', 1)[-1]:
        raise InputError('workload.image: explicit tag or sha256 digest is required')
    namespace = string(work['namespace'], 'workload.namespace', LABEL, 63)
    if namespace in ('default', 'kube-system', 'kube-public', 'kube-node-lease'):
        raise InputError('workload.namespace: reserved namespace')
    replicas = integer(work.get('replicas', 1), 'workload.replicas', 1, 10)
    port = integer(work.get('container_port', 80), 'workload.container_port', 1, 65535)
    health = string(work.get('health_path', '/'), 'workload.health_path', r'/[^\s?#]*', 256)
    sample = work.get('sample_content', False)
    if type(sample) is not bool:
        raise InputError('workload.sample_content: expected boolean')
    if sample and (port != 80 or health != '/' or not re.fullmatch(r'(?:docker.io/library/)?nginx:[A-Za-z0-9_.-]+', image)):
        raise InputError('sample_content is only for tagged nginx on port 80 with health_path /')
    exposure = obj(data.get('exposure', {}), 'exposure', ('type', 'node_port', 'verification_url'))
    if exposure.get('type', 'nodeport') != 'nodeport':
        raise InputError('exposure.type: only nodeport is implemented')
    node_port = integer(exposure.get('node_port', 30080), 'exposure.node_port', 30000, 32767)
    url = exposure.get('verification_url')
    if url is not None:
        string(url, 'exposure.verification_url', maximum=1024)
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in url):
            raise InputError('exposure.verification_url: expected HTTP(S) URL without credentials/query/fragment')
    runtime = obj(data.get('runtime', {}), 'runtime', ('k3s_version', 'cilium_version', 'cilium_cli_version', 'timeout_seconds', 'node_ip'))
    defaults = RuntimeSpec()
    versions = [string(runtime.get(k, getattr(defaults, k)), f'runtime.{k}', pattern, 64) for k, pattern in (
        ('k3s_version', r'v[0-9]+\.[0-9]+\.[0-9]+\+k3s[0-9]+'),
        ('cilium_version', r'[0-9]+\.[0-9]+\.[0-9]+'),
        ('cilium_cli_version', r'v[0-9]+\.[0-9]+\.[0-9]+'))]
    timeout = integer(runtime.get('timeout_seconds', 180), 'runtime.timeout_seconds', 5, 900)
    node_ip = runtime.get('node_ip')
    if node_ip is not None:
        try:
            if not isinstance(node_ip, str):
                raise ValueError()
            ipaddress.IPv4Address(node_ip)
        except ValueError:
            raise InputError('runtime.node_ip: expected IPv4 address') from None
    return AdaptedRequest(DeploymentSpec(environment, WorkloadSpec(image, namespace, replicas, port, health, sample),
                                        RuntimeSpec(*versions, timeout, node_ip), ExposureSpec(node_port, url)),
                          RequestContext(provider, host, user))
