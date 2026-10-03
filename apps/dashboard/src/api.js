export const requests = new Set();

export async function request(path, options = {}, controller = new AbortController()) {
  requests.add(controller);
  const timeout = setTimeout(() => controller.abort(), options.method === 'POST' ? (path.endsWith('/plans') ? 600000 : 120000) : path.endsWith('/logs') ? 60000 : 15000);
  try {
    const response = await fetch(path, { credentials: 'same-origin', ...options, signal: controller.signal, redirect: 'error' });
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
