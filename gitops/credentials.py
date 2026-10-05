#!/usr/bin/env python3
"""Renew the registered, scoped Argo cluster credentials without an admin token."""
import argparse
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection, HTTPSConnection
import ipaddress
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import threading
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from argo import LABEL, native, require

LIFETIME = 21600
MAX_POLICY_BYTES = 131072
RENEWAL_WORKERS = 4
LABELS = {'argocd.argoproj.io/secret-type': 'cluster', 'app.kubernetes.io/managed-by': 'railshot'}
APPLICATION_ID = r'app-[a-f0-9]{24}'


class PolicyCapacityError(ValueError):
    code = 'APPLICATION_CREDENTIAL_POLICY_CAPACITY_EXCEEDED'


def credential_labels_match(labels, target_id, project, namespaces):
    """An app credential may leave Argo discovery, but never widen its app scope."""
    if isinstance(target_id, str) and target_id.startswith('observer-'):
        return (project == '' and namespaces in ([], ['']) and isinstance(labels, dict)
                and labels.get('argocd.argoproj.io/secret-type') == 'railshot-observer'
                and labels.get('app.kubernetes.io/managed-by') == 'railshot')
    application = isinstance(target_id, str) and re.fullmatch(APPLICATION_ID, target_id)
    if application and (project != target_id or namespaces != [target_id]):
        return False
    expected = dict(LABELS)
    if isinstance(labels, dict) and labels.get('argocd.argoproj.io/secret-type') == 'railshot-application':
        if not application:
            return False
        expected['argocd.argoproj.io/secret-type'] = 'railshot-application'
    return isinstance(labels, dict) and all(labels.get(key) == value for key, value in expected.items())


def tls_name(value):
    require(isinstance(value, str), 'registered TLS name must be an RFC1918 IPv4')
    address = ipaddress.IPv4Address(value)
    require(any(address in ipaddress.IPv4Network(network) for network in
                ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')), 'registered TLS name must be an RFC1918 IPv4')
    return value


def validate_policy(policy):
    require(set(policy) == {'version', 'targets'} and policy['version'] == 1, 'invalid renewal policy')
    # This is credential storage, not a quota on successfully deployed apps.
    # Use the worker's existing document bound instead of counting registrations.
    if len(json.dumps(policy).encode()) >= MAX_POLICY_BYTES:
        raise PolicyCapacityError('renewal policy exceeds the 128 KiB document limit')
    targets = policy['targets']
    require(isinstance(targets, list), 'registered targets required')
    for target in targets:
        required = {'secret', 'target_id', 'server', 'project', 'namespaces', 'service_account', 'ca_sha256', 'audiences'}
        require(required <= set(target) <= required | {'tls_server_name', 'previous_scope', 'cluster_read'}, 'invalid registration binding')
        for key in ('secret', 'target_id'):
            require(isinstance(target[key], str) and re.fullmatch(LABEL, target[key]), 'invalid registration name')
        require(isinstance(target['project'], str) and (target['project'] == '' or re.fullmatch(LABEL, target['project'])),
                'invalid registration project')
        require(target['secret'] == 'railshot-' + target['target_id'] and target['project'] != 'default',
                'Railshot project registration required')
        url = urlsplit(target['server'])
        require(url.scheme == 'https' and url.hostname and url.port is not None and 1 <= url.port <= 65535 and not url.username and
                not url.password and not url.path and not url.query and not url.fragment, 'explicit TLS API required')
        if 'tls_server_name' in target:
            tls_name(target['tls_server_name'])
        require(re.fullmatch(r'[a-f0-9]{64}', target['ca_sha256']), 'registered CA fingerprint required')
        namespaces = target['namespaces']
        require(isinstance(namespaces, list) and namespaces and len(set(namespaces)) == len(namespaces) and
                all(isinstance(n, str) and re.fullmatch(LABEL, n) and n not in
                    {'default', 'argocd', 'kube-system', 'kube-public', 'kube-node-lease'} for n in namespaces),
                'dedicated namespaces required')
        if re.fullmatch(APPLICATION_ID, target['target_id']):
            require(target['project'] == target['target_id'] and namespaces == [target['target_id']],
                    'application credential must retain its exact project and namespace')
        sa = target['service_account']
        require(set(sa) == {'name', 'namespace', 'uid'} and sa['namespace'] in namespaces and
                re.fullmatch(LABEL, sa['name']) and re.fullmatch(r'[a-f0-9-]{36}', sa['uid']), 'registered SA required')
        if 'cluster_read' in target:
            require(target['cluster_read'] is True and not re.fullmatch(APPLICATION_ID, target['target_id'])
                    and target['project'] == '', 'cluster read scope requires an environment credential')
        if target['target_id'].startswith('observer-'):
            require(target['target_id'] in ('observer-k3s-aws', 'observer-k3s-gcp') and target.get('cluster_read') is True
                    and target['project'] == '' and namespaces == [sa['namespace']] and sa['name'] == 'railshot-observer'
                    and 'previous_scope' not in target, 'fixed observer registration required')
        if 'previous_scope' in target:
            previous = target['previous_scope']
            require(not re.fullmatch(APPLICATION_ID, target['target_id']) and target['project'] == ''
                    and isinstance(previous, dict) and set(previous) == {'project', 'namespaces'},
                    'only a shared environment cluster may transition scope')
            require(isinstance(previous['project'], str) and previous['project'] != 'default'
                    and (previous['project'] == '' or re.fullmatch(LABEL, previous['project'])), 'invalid previous project')
            old_namespaces = previous['namespaces']
            require(isinstance(old_namespaces, list) and old_namespaces and all(isinstance(n, str) and re.fullmatch(LABEL, n)
                        and n not in {'default', 'argocd', 'kube-system', 'kube-public', 'kube-node-lease'} for n in old_namespaces)
                    and len(set(old_namespaces)) == len(old_namespaces) and sa['namespace'] in old_namespaces
                    and (set(old_namespaces) <= set(namespaces) or set(namespaces) <= set(old_namespaces))
                    and all(re.fullmatch(APPLICATION_ID, n) for n in set(namespaces) ^ set(old_namespaces)),
                    'scope transition must retain its SA and grow or shrink only app namespaces')
            require(previous['project'] != target['project'] or set(old_namespaces) != set(namespaces)
                    or target.get('cluster_read') is True,
                    'scope transition must change project or namespaces')
        require(isinstance(target['audiences'], list) and target['audiences'] and
                len(set(target['audiences'])) == len(target['audiences']) and
                all(isinstance(a, str) and 0 < len(a) < 256 for a in target['audiences']), 'API audiences required')
    require(len({t['secret'] for t in targets}) == len(targets), 'duplicate registration')
    return policy


def claims(token, target, now):
    require(isinstance(token, str) and len(token) < 32768 and
            re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token), 'bounded JWT required')
    middle = token.split('.')[1]
    value = json.loads(base64.b64decode(middle + '=' * (-len(middle) % 4), altchars=b'-_', validate=True))
    sa = target['service_account']
    require(value['sub'] == f"system:serviceaccount:{sa['namespace']}:{sa['name']}" and
            value['kubernetes.io']['namespace'] == sa['namespace'] and
            value['kubernetes.io']['serviceaccount'] == {'name': sa['name'], 'uid': sa['uid']} and
            set(value['aud']) == set(target['audiences']) and value['exp'] > now + 60 and
            value.get('nbf', value['iat']) <= now + 60, 'credential identity or expiry differs')
    return value


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RegisteredHTTPSConnection(HTTPSConnection):
    """Connect to the relay while verifying the registered guest's certificate name."""
    def __init__(self, host, port, *, server_name, context, timeout):
        super().__init__(host, port, context=context, timeout=timeout)
        self.server_name = tls_name(server_name)

    def connect(self):
        HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.server_name)


def customer(server, ca, token, path, document=None, *, server_name=None, timeout=15):
    context = ssl.create_default_context(cadata=ca.decode('ascii'))
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    body = None if document is None else json.dumps(document).encode()
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if server_name is not None:
        endpoint = urlsplit(server)
        require(endpoint.scheme == 'https' and endpoint.hostname and endpoint.port and not endpoint.username and
                not endpoint.password and not endpoint.path and not endpoint.query and not endpoint.fragment, 'explicit TLS API required')
        connection = RegisteredHTTPSConnection(endpoint.hostname, endpoint.port, server_name=server_name, context=context, timeout=timeout)
        try:
            connection.request('GET' if body is None else 'POST', path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status >= 400:
                raise HTTPError(server + path, response.status, 'customer API request failed', {}, None)
            require(response.status in (200, 201), 'customer API request failed')
            raw = response.read(2_000_001)
        finally:
            connection.close()
    else:
        opener = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(context=context), NoRedirect())
        req = request.Request(server + path, data=body, headers=headers)
        with opener.open(req, timeout=timeout) as response:
            require(response.status in (200, 201), 'customer API request failed')
            raw = response.read(2_000_001)
    require(len(raw) <= 2_000_000, 'customer response too large')
    return json.loads(raw)


def platform(*args, document=None):
    # In-cluster kubectl uses only this Pod's projected ServiceAccount identity.
    return json.loads(native(['kubectl', '--request-timeout=20s', '-n', 'argocd', *args], document=document))


def registration(secret, target, now):
    if 'previous_scope' in target or 'cluster_read' in target:
        validate_policy({'version': 1, 'targets': [target]})
    meta = secret['metadata']
    require(secret['kind'] == 'Secret' and meta['name'] == target['secret'] and meta['namespace'] == 'argocd',
            'registration owner differs')
    data = {k: base64.b64decode(v, validate=True).decode() for k, v in secret['data'].items()}
    require(credential_labels_match(meta.get('labels'), target['target_id'], data['project'], data['namespaces'].split(',')),
            'registration owner or application scope differs')
    scopes = [target] + ([target['previous_scope']] if 'previous_scope' in target else [])
    namespaces = data['namespaces'].split(',') if data['namespaces'] else []
    require(data['name'] == target['target_id'] and data['server'] == target['server'] and
            data['clusterResources'] == 'false' and any(data['project'] == scope['project'] and
                (not namespaces if scope.get('cluster_read') else
                 len(namespaces) == len(scope['namespaces']) and set(namespaces) == set(scope['namespaces'])) for scope in scopes),
            'registration scope differs')
    config = json.loads(data['config'])
    tls_fields = {'caData', 'insecure'} | ({'serverName'} if 'tls_server_name' in target else set())
    require(set(config) == {'bearerToken', 'tlsClientConfig'} and
            set(config['tlsClientConfig']) == tls_fields and
            config['tlsClientConfig']['insecure'] is False, 'CA-verified bearer configuration required')
    if 'tls_server_name' in target:
        require(config['tlsClientConfig']['serverName'] == tls_name(target['tls_server_name']), 'registered TLS name differs')
    ca = base64.b64decode(config['tlsClientConfig']['caData'], validate=True)
    require(hashlib.sha256(ca).hexdigest() == target['ca_sha256'], 'registered CA differs')
    claims(config['bearerToken'], target, now)
    return config, ca


def renew(target, now=None, *, on_phase=None):
    def phase(name, **metadata):
        if on_phase:
            on_phase(name, **metadata)

    phase('credential_read')
    now = time.time() if now is None else now
    old = platform('get', 'secret', target['secret'], '-o', 'json')
    config, ca = registration(old, target, now)
    phase('token_request', expires_at=datetime.fromtimestamp(
        claims(config['bearerToken'], target, now)['exp'], timezone.utc).isoformat())
    tls = {'server_name': target['tls_server_name']} if 'tls_server_name' in target else {}
    sa = target['service_account']
    path = f"/api/v1/namespaces/{sa['namespace']}/serviceaccounts/{sa['name']}/token"
    response = customer(target['server'], ca, config['bearerToken'], path, {
        'apiVersion': 'authentication.k8s.io/v1', 'kind': 'TokenRequest',
        'spec': {'audiences': target['audiences'], 'expirationSeconds': LIFETIME}}, **tls)
    token = response['status']['token']
    new_claims = claims(token, target, now)
    expiration = datetime.fromisoformat(response['status']['expirationTimestamp'].replace('Z', '+00:00'))
    require(3 * 3600 <= expiration.timestamp() - now <= LIFETIME + 120 and
            abs(expiration.timestamp() - new_claims['exp']) < 2 and
            0 < new_claims['exp'] - new_claims['iat'] <= LIFETIME + 120, 'bounded six-hour renewal required')
    # Decoding JWT claims is only a binding check. The API authenticates the new
    # token against the original CA and proves the exact principal and scope.
    phase('token_authentication')
    who = customer(target['server'], ca, token, '/apis/authentication.k8s.io/v1/selfsubjectreviews',
                   {'apiVersion': 'authentication.k8s.io/v1', 'kind': 'SelfSubjectReview'}, **tls)['status']['userInfo']
    require(who['username'] == new_claims['sub'] and who['uid'] == sa['uid'], 'new token authentication differs')
    # A fixed environment reader no longer enumerates per-app cache scopes.
    # Its original SA namespace remains the token renewal authority.
    phase('scope_verification')
    for namespace in ([sa['namespace']] if target.get('cluster_read') else target['namespaces']):
        pods = customer(target['server'], ca, token, f'/api/v1/namespaces/{namespace}/pods?limit=1', **tls)
        require(pods['kind'] == 'PodList' and isinstance(pods['items'], list), 'namespace read verification failed')
    updated = copy.deepcopy(config); updated['bearerToken'] = token
    encoded = base64.b64encode(json.dumps(updated, separators=(',', ':')).encode()).decode()
    patch = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': old['metadata']['resourceVersion']},
             {'op': 'replace', 'path': '/data/config', 'value': encoded}]
    phase('credential_patch')
    try:
        platform('patch', 'secret', target['secret'], '--type=json', '--patch-file=/dev/stdin', '-o', 'json', document=patch)
    except Exception:
        # The write may already have arrived. Read back once; never replay it.
        pass
    phase('credential_readback')
    try:
        observed = platform('get', 'secret', target['secret'], '-o', 'json')
        expected = {**old['data'], 'config': encoded}
        require(observed['metadata']['uid'] == old['metadata']['uid'], 'registration was replaced')
        registration(observed, target, now)
        if observed['data'] == expected:
            return {'secret': target['secret'], 'status': 'renewed', 'expires_at': expiration.isoformat()}
        if observed['data'] == old['data']:
            return {'secret': target['secret'], 'status': 'unchanged', 'code': 'PATCH_NOT_APPLIED'}
    except Exception:
        pass
    return {'secret': target['secret'], 'status': 'unknown', 'code': 'RENEWAL_READBACK_UNKNOWN'}


def command_environments(command):
    prefix = ['python3', '/app/gitops/credentials.py', 'renew', '--policy', '/etc/railshot/credentials/policy.json']
    require(isinstance(command, list) and command[:len(prefix)] == prefix, 'renewal command differs')
    suffix = command[len(prefix):]
    require(len(suffix) % 2 == 0 and all(suffix[i] == '--environment' for i in range(0, len(suffix), 2)), 'renewal scope arguments differ')
    values = suffix[1::2]
    require(all(isinstance(v, str) and re.fullmatch(LABEL, v) and not v.startswith('app-') for v in values)
            and len(set(values)) == len(values), 'renewal environments differ')
    return values


def render(policy, image, *, platform_arch='amd64', local_provenance=None, environments=()):
    select_policy(policy, environments)
    require(platform_arch in ('amd64', 'arm64'), 'supported platform architecture required')
    local = local_provenance is not None
    if local:
        require(isinstance(local_provenance, dict) and set(local_provenance) == {
            'source_sha256', 'oci_archive_sha256', 'manifest_digest', 'image_id'},
            'exact local image provenance required')
        require(all(isinstance(local_provenance[key], str) and re.fullmatch(r'[a-f0-9]{64}', local_provenance[key])
                    for key in ('source_sha256', 'oci_archive_sha256')), 'local content hashes required')
        require(all(isinstance(local_provenance[key], str) and re.fullmatch(r'sha256:[a-f0-9]{64}', local_provenance[key])
                    for key in ('manifest_digest', 'image_id')), 'local OCI digests required')
        match = re.fullmatch(r'localhost/railshot-api@(sha256:[a-f0-9]{64})', image or '')
        require(match and match.group(1) == local_provenance['manifest_digest'],
                'imported local API manifest digest required')
    else:
        require(re.fullmatch(r'ghcr\.io/jasmin-softbank/railshot-api@sha256:[a-f0-9]{64}', image or ''),
                'published API digest required')
    name = 'railshot-credentials'
    meta = {'name': name, 'namespace': 'argocd'}
    sa_path = '/var/run/secrets/kubernetes.io/serviceaccount/'
    kubeconfig = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': name,
                  'clusters': [{'name': 'platform', 'cluster': {'server': 'https://kubernetes.default.svc:443',
                               'certificate-authority': sa_path + 'ca.crt'}}],
                  'users': [{'name': name, 'user': {'tokenFile': sa_path + 'token'}}],
                  'contexts': [{'name': name, 'context': {'cluster': 'platform', 'user': name, 'namespace': 'argocd'}}]}
    items = [
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': meta, 'automountServiceAccountToken': False},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': meta, 'rules': [
            {'apiGroups': [''], 'resources': ['secrets'], 'resourceNames': [t['secret'] for t in policy['targets']],
             'verbs': ['get', 'patch']}] if policy['targets'] else []},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': meta,
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': name},
         'subjects': [{'kind': 'ServiceAccount', 'name': name, 'namespace': 'argocd'}]},
        {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': meta,
         'data': {'policy.json': json.dumps(policy), 'kubeconfig': json.dumps(kubeconfig)}}]
    pod = {'serviceAccountName': name, 'automountServiceAccountToken': True, 'restartPolicy': 'Never',
           'nodeSelector': {'kubernetes.io/arch': platform_arch, 'railshot.io/node-role': 'platform'},
           'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                               'seccompProfile': {'type': 'RuntimeDefault'}},
           'containers': [{'name': 'renew', 'image': image,
                           'command': ['python3', '/app/gitops/credentials.py', 'renew', '--policy', '/etc/railshot/credentials/policy.json']
                                      + [value for environment in environments for value in ('--environment', environment)],
                           'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                               'capabilities': {'drop': ['ALL']}},
                           'resources': {'requests': {'cpu': '10m', 'memory': '64Mi'}, 'limits': {'cpu': '200m', 'memory': '128Mi'}},
                           'env': [{'name': 'HOME', 'value': '/tmp'},
                                   {'name': 'KUBECONFIG', 'value': '/etc/railshot/credentials/kubeconfig'}],
                           'volumeMounts': [{'name': 'policy', 'mountPath': '/etc/railshot/credentials', 'readOnly': True},
                                            {'name': 'tmp', 'mountPath': '/tmp'}]}],
           'volumes': [{'name': 'policy', 'configMap': {'name': name}}, {'name': 'tmp', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '16Mi'}}]}
    template = {'spec': pod}
    if local:
        pod.update(hostNetwork=True, dnsPolicy='ClusterFirstWithHostNet')
        pod['containers'][0]['imagePullPolicy'] = 'Never'
        template['metadata'] = {'annotations': {
            'railshot.io/local-source-sha256': local_provenance['source_sha256'],
            'railshot.io/local-oci-archive-sha256': local_provenance['oci_archive_sha256'],
            'railshot.io/local-manifest-digest': local_provenance['manifest_digest'],
            'railshot.io/local-image-id': local_provenance['image_id']}}
    else:
        pod['imagePullSecrets'] = [{'name': 'ghcr-pull'}]
    items.append({'apiVersion': 'batch/v1', 'kind': 'CronJob', 'metadata': meta,
                  'spec': {'schedule': '0 */2 * * *', 'timeZone': 'Etc/UTC', 'concurrencyPolicy': 'Forbid',
                           'startingDeadlineSeconds': 600, 'successfulJobsHistoryLimit': 1, 'failedJobsHistoryLimit': 3,
                           'jobTemplate': {'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 300,
                                                    'template': template}}}})
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items}


def select_policy(policy, environments):
    """Select registered environment servers, including their private app credentials."""
    validate_policy(policy)
    if not environments:
        return policy
    require(len(environments) == len(set(environments)), 'duplicate renewal environment')
    servers = set()
    for environment in environments:
        rows = [row for row in policy['targets'] if row['target_id'] == environment]
        require(len(rows) == 1 and not re.fullmatch(APPLICATION_ID, environment),
                'registered renewal environment required')
        servers.add(rows[0]['server'])
    return {**policy, 'targets': [row for row in policy['targets'] if row['server'] in servers]}


def renewal_error(exc):
    """Stable codes only; native diagnostics and HTTP response bodies are private."""
    reason = exc.reason if isinstance(exc, URLError) else exc.__cause__ or exc
    if isinstance(reason, (TimeoutError, subprocess.TimeoutExpired)):
        return 'RENEWAL_TIMEOUT'
    if isinstance(reason, ssl.SSLError):
        return 'CUSTOMER_TLS_FAILED'
    if isinstance(exc, HTTPError):
        return 'CUSTOMER_AUTH_REJECTED' if exc.code in (401, 403) else 'CUSTOMER_API_FAILED'
    if isinstance(exc, URLError):
        return 'CUSTOMER_API_UNREACHABLE'
    return 'RENEWAL_FAILED'


def renew_policy(policy, *, report=None):
    """Renew independent Secrets with bounded concurrency; keep failures isolated."""
    validate_policy(policy)
    report_lock = threading.Lock()

    def attempt(target):
        started = time.monotonic()
        current = {'phase': 'credential_read'}

        def emit(event, **values):
            if report:
                with report_lock:
                    report({'event': event, 'checked_at': datetime.now(timezone.utc).isoformat(),
                        'target_id': target['target_id'], 'secret': target['secret'],
                        'duration_ms': round((time.monotonic() - started) * 1000), **current, **values})

        def phase(name, **metadata):
            current.update(phase=name, **metadata)
            emit('renewal_phase')

        emit('renewal_started')
        try:
            result = renew(target, on_phase=phase) if report else renew(target)
        except Exception as exc:
            result = {'secret': target['secret'], 'status': 'unchanged', 'code': renewal_error(exc)}
        emit('renewal_finished', **{k: v for k, v in result.items() if k != 'secret'})
        return result

    # Policy validation rejects duplicate Secrets. Each worker retains the
    # existing resourceVersion check and readback; it never retries a write.
    with ThreadPoolExecutor(max_workers=RENEWAL_WORKERS) as executor:
        return list(executor.map(attempt, policy['targets']))


def observe_environment(environment, context, *, now=None):
    """Read two environment credentials and authenticate them; never issue a token."""
    now = time.time() if now is None else now
    result = {'state': 'collection_failed', 'last_success_at': None, 'expires_at': None,
              'checked_at': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'reason': None}
    try:
        require(environment in ('k3s-aws', 'k3s-gcp') and isinstance(context, str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,200}', context), 'registered observation environment required')
        def read(kind, name):
            return json.loads(native(['kubectl', '--context', context, '--request-timeout=4s', '-n', 'argocd',
                                     'get', kind, name, '-o', 'json'], timeout=5))
        policy = validate_policy(json.loads(read('configmap', 'railshot-credentials')['data']['policy.json']))
        rows = [row for row in policy['targets'] if row['target_id'] in (environment, 'observer-' + environment)]
        if len(rows) != 2:
            return {**result, 'state': 'no_data', 'reason': 'ENVIRONMENT_OBSERVER_NOT_REGISTERED'}
        require(len({row['server'] for row in rows}) == 1, 'credential environment differs')
        def inspect(row):
            config, ca = registration(read('secret', row['secret']), row, now)
            issued = claims(config['bearerToken'], row, now)
            tls = {'server_name': row['tls_server_name']} if 'tls_server_name' in row else {}
            who = customer(row['server'], ca, config['bearerToken'], '/apis/authentication.k8s.io/v1/selfsubjectreviews',
                {'apiVersion': 'authentication.k8s.io/v1', 'kind': 'SelfSubjectReview'}, timeout=4, **tls)['status']['userInfo']
            require(who['username'] == issued['sub'] and who['uid'] == row['service_account']['uid'], 'credential authentication differs')
            return issued
        with ThreadPoolExecutor(max_workers=2) as executor:
            verified = list(executor.map(inspect, rows))
        return {**result, 'state': 'ready',
                'last_success_at': datetime.fromtimestamp(min(row['iat'] for row in verified), timezone.utc).isoformat(),
                'expires_at': datetime.fromtimestamp(min(row['exp'] for row in verified), timezone.utc).isoformat()}
    except Exception as exc:
        return {**result, 'reason': renewal_error(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('renew', 'render', 'observe'))
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--context', help='operator kube context for read-only observation')
    parser.add_argument('--environment', action='append', default=[], help='renew only this registered environment and its apps; repeat for each environment')
    parser.add_argument('--image', help='published existing API image digest, for render only')
    args = parser.parse_args()
    try:
        if args.command == 'observe':
            require(len(args.environment) == 1, 'one observation environment required')
            print(json.dumps(observe_environment(args.environment[0], args.context)), flush=True); return 0
        require(args.policy is not None, 'renewal policy required')
        require(args.policy.stat().st_size < MAX_POLICY_BYTES, 'small operator policy required')
        policy = validate_policy(json.loads(args.policy.read_bytes()))
        if args.command == 'render':
            print(json.dumps(render(policy, args.image or '', environments=args.environment), indent=2)); return 0
        selected = select_policy(policy, args.environment)
        started = time.monotonic()
        results = renew_policy(selected, report=lambda event: print(json.dumps(event), flush=True))
        renewed = sum(row['status'] == 'renewed' for row in results)
        print(json.dumps({'event': 'renewal_summary', 'checked_at': datetime.now(timezone.utc).isoformat(),
                          'status': 'succeeded' if renewed == len(results) else 'failed',
                          'environments': args.environment, 'selected_count': len(results),
                          'excluded_count': len(policy['targets']) - len(results),
                          'renewed_count': renewed, 'failed_count': len(results) - renewed,
                          'duration_ms': round((time.monotonic() - started) * 1000),
                          'results': results}), flush=True)
        return 0 if renewed == len(results) else 1
    except Exception:
        print(json.dumps({'status': 'blocked', 'code': 'INVALID_RENEWAL_POLICY'}), flush=True); return 2


if __name__ == '__main__':
    sys.exit(main())
