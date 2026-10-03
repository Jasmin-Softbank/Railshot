export const requests = new Set();

export async function request(path, options = {}, controller = new AbortController()) {
  requests.add(controller);
  const asynchronousPlan = path.endsWith('/plans') && options.headers?.Prefer === 'respond-async';
  const timeout = setTimeout(() => controller.abort(), asynchronousPlan ? 15000 : options.method === 'POST' ? (path.endsWith('/plans') ? 600000 : 120000) : path.endsWith('/logs') ? 60000 : 15000);
  try {
    const response = await fetch(path, { credentials: 'same-origin', ...options, signal: controller.signal, redirect: 'error' });
    if (response.status !== 204 && !response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('API가 JSON 대신 다른 응답을 반환했습니다. API 서버와 프록시 경로를 확인하세요.');
    }
    const data = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      const failure = new Error(data.error?.message || (typeof data.error === 'string' ? data.error : `요청 실패 (HTTP ${response.status})`));
      Object.assign(failure, { status: response.status, code: data.error?.code,
        outcomeUnknown: data.error?.outcome_unknown, admission: data.error?.admission });
      throw failure;
    }
    return { data, location: response.headers.get('location'), status: response.status };
  } finally { clearTimeout(timeout); requests.delete(controller); }
}
