"""등록 비밀은 요청 헤더에서만 사용하며 응답 오류 본문은 노출하지 않습니다."""
import base64
import ipaddress
import json
import re
import ssl
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('등록 요청의 리다이렉트를 허용하지 않습니다.')


def validate_key(value):
    if not isinstance(value, str):
        raise ValueError('WireGuard 공개키 형식이 올바르지 않습니다.')
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('WireGuard 공개키 형식이 올바르지 않습니다.') from exc
    if len(decoded) != 32 or base64.b64encode(decoded).decode() != value:
        raise ValueError('WireGuard 공개키 길이가 올바르지 않습니다.')
    return value


def validate_https_url(value):
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
        raise ValueError('HTTPS 주소가 올바르지 않습니다.')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('인증정보 없는 HTTPS 주소가 필요합니다.')
    _ = parsed.port
    return value


def validate_registration(data):
    required = {'node_id', 'address', 'server_public_key', 'endpoint', 'allowed_ips', 'probe_url'}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError('등록 응답 필드가 규격과 다릅니다.')
    if not isinstance(data['node_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data['node_id']):
        raise ValueError('노드 식별자가 올바르지 않습니다.')
    address = ipaddress.ip_interface(data['address'])
    if address.network.prefixlen != address.max_prefixlen or address.ip.is_unspecified or address.ip.is_multicast or address.ip.is_loopback:
        raise ValueError('터널 주소가 올바르지 않습니다.')
    validate_key(data['server_public_key'])
    endpoint = data['endpoint']
    if not isinstance(endpoint, str) or not re.fullmatch(r'(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9a-fA-F:]+\]):[0-9]{1,5}', endpoint):
        raise ValueError('WireGuard 접속 주소가 올바르지 않습니다.')
    host, port = endpoint.rsplit(':', 1)
    if not 1 <= int(port) <= 65535:
        raise ValueError('WireGuard 포트가 올바르지 않습니다.')
    if host.startswith('['):
        ipaddress.IPv6Address(host[1:-1])
    allowed = data['allowed_ips']
    if not isinstance(allowed, list) or not 1 <= len(allowed) <= 16:
        raise ValueError('허용 주소 목록이 올바르지 않습니다.')
    networks = [ipaddress.ip_network(item, strict=True) for item in allowed]
    if any(n.prefixlen != n.max_prefixlen or n.network_address.is_loopback or n.network_address.is_multicast or n.network_address.is_unspecified for n in networks):
        raise ValueError('터널 상대 주소는 /32 또는 /128 호스트 경로만 허용합니다.')
    validate_https_url(data['probe_url'])
    parsed = urlsplit(data['probe_url'])
    probe = ipaddress.ip_address(parsed.hostname)  # DNS를 통한 터널 우회 방지
    if parsed.query or not any(probe in network for network in networks):
        raise ValueError('연결 점검 주소는 터널 허용 IP 범위 안이어야 합니다.')
    return dict(data)


def enroll_client(base_url, token, public_key, request_id, *, opener=None):
    validate_https_url(base_url)
    if urlsplit(base_url).query:
        raise ValueError('등록 주소에는 쿼리를 포함할 수 없습니다.')
    validate_key(public_key)
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{16,4096}', token):
        raise ValueError('일회용 등록 키 형식이 올바르지 않습니다.')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_id):
        raise ValueError('영속화한 등록 요청 식별자가 필요합니다.')
    body = json.dumps({'public_key': public_key, 'request_id': request_id}).encode()
    request = Request(base_url.rstrip('/') + '/v1/enrollments', data=body,
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                               'Idempotency-Key': request_id}, method='POST')
    opener = opener or build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError('등록 요청에 실패했습니다.')
            raw = response.read(65537)
            if len(raw) > 65536:
                raise RuntimeError('등록 응답 크기를 초과했습니다.')
            data = json.loads(raw)
    except Exception:
        raise RuntimeError('서비스 등록에 실패했습니다. 같은 요청 식별자로 재시도하세요.') from None
    return validate_registration(data)
