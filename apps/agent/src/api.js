import { readFileSync } from 'node:fs';

function apiToken(env) {
  if (env.RAILSHOT_API_TOKEN && env.RAILSHOT_API_TOKEN_FILE) throw new Error('API 토큰은 값 또는 파일 중 하나만 설정하세요.');
  if (!env.RAILSHOT_API_TOKEN_FILE) return env.RAILSHOT_API_TOKEN;
  try { return readFileSync(env.RAILSHOT_API_TOKEN_FILE, 'utf8').trim(); }
  catch { throw new Error('API 토큰 파일을 읽을 수 없습니다.'); }
}

export function createApiClient({ baseUrl = process.env.RAILSHOT_API_URL || 'http://127.0.0.1:4173', env = process.env, fetchImpl = fetch } = {}) {
  const base = new URL(baseUrl);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash || base.pathname !== '/') {
    throw new Error('RAILSHOT_API_URL은 API 원점 주소여야 합니다.');
  }
  async function request(path, { method = 'GET', body, key } = {}) {
    const token = apiToken(env);
    const headers = { ...(token ? { authorization: `Bearer ${token}` } : {}), ...(key ? { 'Idempotency-Key': key } : {}) };
    const response = await fetchImpl(new URL(path, base), {
      method, headers, body, redirect: 'error', signal: AbortSignal.timeout(30000),
    });
    let value;
    try { value = await response.json(); } catch { throw new Error(`API 응답을 읽을 수 없습니다 (${response.status}).`); }
    if (!response.ok) {
      const detail = value?.error;
      const error = new Error(typeof detail?.message === 'string' ? detail.message : `API 요청 실패 (${response.status}).`);
      error.code = detail?.code || 'UPSTREAM_FAILURE';
      error.status = response.status;
      error.outcomeUnknown = detail?.outcome_unknown === true;
      throw error;
    }
    return value;
  }
  return {
    options: () => request('/api/v1/options'),
    targets: () => request('/api/v1/targets'),
    deployment: (id) => request(`/api/v1/deployments/${encodeURIComponent(id)}`),
    build: (id) => request(`/api/v1/builds/${encodeURIComponent(id)}`),
    deploy: ({ repository_url, app, target_id, idempotency_key }) => {
      const form = new FormData();
      form.set('repository_url', repository_url);
      form.set('app', app);
      form.set('target_id', target_id);
      return request('/api/v1/deployments', { method: 'POST', body: form, key: idempotency_key });
    },
  };
}
