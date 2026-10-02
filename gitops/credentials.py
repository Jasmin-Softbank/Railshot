#!/usr/bin/env python3
"""Renew the registered, scoped Argo cluster credentials without an admin token."""
import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection, HTTPSConnection
import ipaddress
import json
from pathlib import Path
import re
import ssl
import sys
import time
from urllib import request
from urllib.parse import urlsplit

from argo import LABEL, native, require

LIFETIME = 21600
LABELS = {'argocd.argoproj.io/secret-type': 'cluster', 'app.kubernetes.io/managed-by': 'railshot'}


def tls_name(value):
    require(isinstance(value, str), 'registered TLS name must be an RFC1918 IPv4')
    address = ipaddress.IPv4Address(value)
    require(any(address in ipaddress.IPv4Network(network) for network in
                ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')), 'registered TLS name must be an RFC1918 IPv4')
    return value


def validate_policy(policy):
    require(set(policy) == {'version', 'targets'} and policy['version'] == 1, 'invalid renewal policy')
    targets = policy['targets']
    require(isinstance(targets, list) and 1 <= len(targets) <= 20, 'registered targets required')
    for target in targets:
        required = {'secret', 'target_id', 'server', 'project', 'namespaces', 'service_account', 'ca_sha256', 'audiences'}
        require(required <= set(target) <= required | {'tls_server_name'}, 'invalid registration binding')
        for key in ('secret', 'target_id', 'project'):
            require(isinstance(target[key], str) and re.fullmatch(LABEL, target[key]), 'invalid registration name')
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
        sa = target['service_account']
        require(set(sa) == {'name', 'namespace', 'uid'} and sa['namespace'] in namespaces and
                re.fullmatch(LABEL, sa['name']) and re.fullmatch(r'[a-f0-9-]{36}', sa['uid']), 'registered SA required')
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


def customer(server, ca, token, path, document=None, *, server_name=None):
    context = ssl.create_default_context(cadata=ca.decode('ascii'))
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    body = None if document is None else json.dumps(document).encode()
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if server_name is not None:
        endpoint = urlsplit(server)
        require(endpoint.scheme == 'https' and endpoint.hostname and endpoint.port and not endpoint.username and
                not endpoint.password and not endpoint.path and not endpoint.query and not endpoint.fragment, 'explicit TLS API required')
        connection = RegisteredHTTPSConnection(endpoint.hostname, endpoint.port, server_name=server_name, context=context, timeout=15)
        try:
            connection.request('GET' if body is None else 'POST', path, body=body, headers=headers)
            response = connection.getresponse()
            require(response.status in (200, 201), 'customer API request failed')
            raw = response.read(2_000_001)
        finally:
            connection.close()
    else:
        opener = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(context=context), NoRedirect())
        req = request.Request(server + path, data=body, headers=headers)
        with opener.open(req, timeout=15) as response:
            require(response.status in (200, 201), 'customer API request failed')
            raw = response.read(2_000_001)
    require(len(raw) <= 2_000_000, 'customer response too large')
    return json.loads(raw)


def platform(*args, document=None):
    # In-cluster kubectl uses only this Pod's projected ServiceAccount identity.
    return json.loads(native(['kubectl', '--request-timeout=20s', '-n', 'argocd', *args], document=document))


def registration(secret, target, now):
    meta = secret['metadata']
    require(secret['kind'] == 'Secret' and meta['name'] == target['secret'] and meta['namespace'] == 'argocd' and
            all(meta.get('labels', {}).get(k) == v for k, v in LABELS.items()), 'registration owner differs')
    data = {k: base64.b64decode(v, validate=True).decode() for k, v in secret['data'].items()}
    require(data['name'] == target['target_id'] and data['server'] == target['server'] and
            data['project'] == target['project'] and data['clusterResources'] == 'false' and
            set(data['namespaces'].split(',')) == set(target['namespaces']), 'registration scope differs')
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


def renew(target, now=None):
    now = time.time() if now is None else now
    old = platform('get', 'secret', target['secret'], '-o', 'json')
    config, ca = registration(old, target, now)
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
    who = customer(target['server'], ca, token, '/apis/authentication.k8s.io/v1/selfsubjectreviews',
                   {'apiVersion': 'authentication.k8s.io/v1', 'kind': 'SelfSubjectReview'}, **tls)['status']['userInfo']
    require(who['username'] == new_claims['sub'] and who['uid'] == sa['uid'], 'new token authentication differs')
    for namespace in target['namespaces']:
        pods = customer(target['server'], ca, token, f'/api/v1/namespaces/{namespace}/pods?limit=1', **tls)
        require(pods['kind'] == 'PodList' and isinstance(pods['items'], list), 'namespace read verification failed')
    updated = copy.deepcopy(config); updated['bearerToken'] = token
    encoded = base64.b64encode(json.dumps(updated, separators=(',', ':')).encode()).decode()
    patch = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': old['metadata']['resourceVersion']},
             {'op': 'replace', 'path': '/data/config', 'value': encoded}]
    try:
        platform('patch', 'secret', target['secret'], '--type=json', '--patch-file=/dev/stdin', '-o', 'json', document=patch)
    except Exception:
        # The write may already have arrived. Read back once; never replay it.
        pass
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


def render(policy, image):
    validate_policy(policy)
    require(re.fullmatch(r'ghcr\.io/jasmin-softbank/railshot-api@sha256:[a-f0-9]{64}', image), 'published API digest required')
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
             'verbs': ['get', 'patch']}]},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': meta,
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': name},
         'subjects': [{'kind': 'ServiceAccount', 'name': name, 'namespace': 'argocd'}]},
        {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': meta,
         'data': {'policy.json': json.dumps(policy), 'kubeconfig': json.dumps(kubeconfig)}}]
    pod = {'serviceAccountName': name, 'automountServiceAccountToken': True, 'restartPolicy': 'Never',
           'nodeSelector': {'kubernetes.io/arch': 'amd64', 'railshot.io/node-role': 'platform'},
           'imagePullSecrets': [{'name': 'ghcr-pull'}],
           'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                               'seccompProfile': {'type': 'RuntimeDefault'}},
           'containers': [{'name': 'renew', 'image': image,
                           'command': ['python3', '/app/gitops/credentials.py', 'renew', '--policy', '/etc/railshot/credentials/policy.json'],
                           'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                               'capabilities': {'drop': ['ALL']}},
                           'resources': {'requests': {'cpu': '10m', 'memory': '64Mi'}, 'limits': {'cpu': '200m', 'memory': '128Mi'}},
                           'env': [{'name': 'HOME', 'value': '/tmp'},
                                   {'name': 'KUBECONFIG', 'value': '/etc/railshot/credentials/kubeconfig'}],
                           'volumeMounts': [{'name': 'policy', 'mountPath': '/etc/railshot/credentials', 'readOnly': True},
                                            {'name': 'tmp', 'mountPath': '/tmp'}]}],
           'volumes': [{'name': 'policy', 'configMap': {'name': name}}, {'name': 'tmp', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '16Mi'}}]}
    items.append({'apiVersion': 'batch/v1', 'kind': 'CronJob', 'metadata': meta,
                  'spec': {'schedule': '0 */2 * * *', 'timeZone': 'Etc/UTC', 'concurrencyPolicy': 'Forbid',
                           'startingDeadlineSeconds': 600, 'successfulJobsHistoryLimit': 1, 'failedJobsHistoryLimit': 3,
                           'jobTemplate': {'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 300,
                                                    'template': {'spec': pod}}}}})
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('renew', 'render'))
    parser.add_argument('--policy', required=True, type=Path)
    parser.add_argument('--image', help='published existing API image digest, for render only')
    args = parser.parse_args()
    try:
        require(args.policy.stat().st_size < 131072, 'small operator policy required')
        policy = validate_policy(json.loads(args.policy.read_bytes()))
        if args.command == 'render':
            print(json.dumps(render(policy, args.image or ''), indent=2)); return 0
        results = []
        for target in policy['targets']:
            try:
                result = renew(target)
            except Exception:
                result = {'secret': target['secret'], 'status': 'unchanged', 'code': 'RENEWAL_FAILED'}
            results.append(result)
        print(json.dumps({'results': results}))
        return 0 if all(r['status'] == 'renewed' for r in results) else 1
    except Exception:
        print(json.dumps({'status': 'blocked', 'code': 'INVALID_RENEWAL_POLICY'})); return 2


if __name__ == '__main__':
    sys.exit(main())
