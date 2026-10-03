export const requests = new Set();

export async function request(path, options = {}, controller = new AbortController()) {
  requests.add(controller);
  const asynchronousPlan = path.endsWith('/plans') && options.headers?.Prefer === 'respond-async';
  const timeout = setTimeout(() => controller.abort(), asynchronousPlan ? 15000 : options.method === 'POST' ? (path.endsWith('/plans') ? 600000 : 120000) : path.endsWith('/logs') ? 60000 : 15000);
  try {
    const response = await fetch(path, { credentials: 'same-origin', ...options, signal: controller.signal, redirect: 'error' });
    let data;
    try { data = response.status === 204 ? null : await response.json(); }
    catch (cause) {
      if (controller.signal.aborted) throw cause;
      // A proxy HTML error does not prove that a submitted operation was rejected.
      throw Object.assign(new Error(`서버 응답을 확인하지 못했습니다 (HTTP ${response.status}). 잠시 후 상태를 다시 확인하세요.`),
        { status: response.status, code: 'RESPONSE_UNREADABLE' });
    }
    if (!response.ok) {
      const failure = new Error(data?.error?.message || (typeof data?.error === 'string' ? data.error : `요청 실패 (HTTP ${response.status})`));
      Object.assign(failure, { status: response.status, code: data?.error?.code,
        outcomeUnknown: data?.error?.outcome_unknown, admission: data?.error?.admission });
      throw failure;
    }
    return { data, location: response.headers.get('location'), status: response.status };
  } finally { clearTimeout(timeout); requests.delete(controller); }
}
