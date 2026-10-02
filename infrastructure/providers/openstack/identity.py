"""Keystone provisioning: all passwords and tokens stay on the customer node."""
import json
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from .cli import ProviderError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Keystone:
    def __init__(self, endpoint, timeout=30):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('auth_url must be an HTTPS Keystone v3 endpoint')
        self.endpoint = endpoint.rstrip('/')
        if not self.endpoint.endswith('/v3'):
            raise ValueError('auth_url must end with /v3')
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, method, path, body=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Auth-Token'] = token
        request = urllib.request.Request(self.endpoint + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ProviderError('identity_response_too_large')
                return (json.loads(raw) if raw else {}, response.headers.get('X-Subject-Token'))
        except urllib.error.HTTPError as exc:
            code = {401:'unauthorized',403:'forbidden',404:'not_found',409:'conflict'}.get(exc.code, 'identity_failed')
            raise ProviderError(code) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise ProviderError('identity_connection_or_response_failed') from None


def _segment(value):
    return urllib.parse.quote(str(value), safe='')


def _field(value, *path):
    try:
        for key in path:
            value = value[key]
        if not isinstance(value, str) or not value:
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError):
        raise ProviderError('identity_invalid_response') from None


def configure_identity(config, admin_password, on_resource=lambda resource: None, *, keystone_factory=Keystone):
    required = ('auth_url', 'admin_username', 'user_domain_name', 'project_id', 'service_username', 'role_id')
    for key in required:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError('Missing identity setting: ' + key)
    if not admin_password:
        raise ValueError('Administrator password required')
    client = keystone_factory(config['auth_url'])
    password_auth = {'methods': ['password'], 'password': {'user': {
        'name': config['admin_username'], 'domain': {'name': config['user_domain_name']}, 'password': admin_password}}}
    # Obtain unscoped identity once; never persist this short-lived token.
    unscoped, _ = client.request('POST', '/auth/tokens', {'auth': {'identity': password_auth, 'scope': 'unscoped'}})
    domain_id = _field(unscoped, 'token','user','domain','id')
    admin_scope = config.get('admin_scope', {'project': {'id': config.get('admin_project_id', config['project_id'])}})
    if not isinstance(admin_scope, dict) or len(admin_scope) != 1 or not set(admin_scope) <= {'system','domain','project'}:
        raise ValueError('admin_scope must explicitly select system, domain or project')
    scoped, admin_token = client.request('POST', '/auth/tokens', {'auth': {'identity': password_auth,
        'scope': admin_scope}})
    if not isinstance(admin_token,str) or not admin_token:
        raise ProviderError('identity_invalid_response')
    query = urllib.parse.urlencode({'domain_id': domain_id, 'name': config['service_username'], 'domain_id': domain_id})
    existing, _ = client.request('GET', '/users?' + query, token=admin_token)
    if not isinstance(existing.get('users'),list):
        raise ProviderError('identity_invalid_response')
    if existing.get('users'):
        raise ProviderError('existing_service_user', 'Existing service user is never adopted or reset; reconcile recorded resources first')
    service_password = secrets.token_urlsafe(48)
    created, _ = client.request('POST', '/users', {'user': {'name': config['service_username'],
        'domain_id': domain_id, 'password': service_password, 'enabled': True,
        'description': 'Railshot customer-local bootstrap account'}}, token=admin_token)
    user_id = _field(created,'user','id')
    on_resource({'type': 'user', 'id': user_id, 'name': config['service_username'], 'domain_id': domain_id})
    path = '/projects/{}/users/{}/roles/{}'.format(*map(_segment, (config['project_id'],user_id,config['role_id'])))
    client.request('PUT', path, token=admin_token)
    on_resource({'type': 'role_assignment', 'user_id': user_id, 'project_id': config['project_id'], 'role_id': config['role_id']})
    service, service_token = client.request('POST', '/auth/tokens', {'auth': {'identity': {
        'methods': ['password'], 'password': {'user': {'id': user_id, 'password': service_password}}},
        'scope': {'project': {'id': config['project_id']}}}})
    if not isinstance(service_token,str) or not service_token or _field(service,'token','project','id') != config['project_id']:
        raise ProviderError('project_mismatch')
    credential, _ = client.request('POST', '/users/' + _segment(user_id) + '/application_credentials',
        {'application_credential': {'name': 'jasmin-bootstrap', 'roles': [{'id': config['role_id']}], 'unrestricted': False}}, token=service_token)
    app = {'id':_field(credential,'application_credential','id'), 'secret':_field(credential,'application_credential','secret')}
    on_resource({'type': 'application_credential', 'id': app['id'], 'user_id': user_id})
    auth = {'auth_url': config['auth_url'], 'application_credential_id': app['id'], 'application_credential_secret': app['secret']}
    verified, _ = client.request('POST', '/auth/tokens', {'auth': {'identity': {'methods': ['application_credential'],
        'application_credential': {'id': app['id'], 'secret': app['secret']}}}})
    if _field(verified,'token','project','id') != config['project_id']:
        raise ProviderError('project_mismatch')
    for option in ('region_name', 'interface'):
        if config.get(option):
            auth[option] = config[option]
    return auth
