// Runs as an Argo PreSync Job using the incoming API image on the current API node.
// Starting this process proves image pull finished before the live Pod is touched.
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { setTimeout as pause } from 'node:timers/promises';
import { readApiToken } from './access.js';

export async function prepareRelease({ token, templateId, url = 'http://railshot-api:4173/internal/releases/prepare',
  timeoutMs = 840_000, pollMs = 2000, request = fetch } = {}) {
  if (!token || !/^[a-f0-9]{64}$/.test(templateId || '')) throw new Error('RELEASE_CONFIGURATION_REQUIRED');
  const release_id = randomUUID(), deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await request(url, { method: 'POST', redirect: 'error',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', 'x-railshot-desired-template': templateId },
      body: JSON.stringify({ release_id }), signal: AbortSignal.timeout(Math.min(5000, timeoutMs)),
    });
    const result = await response.json();
    if (response.status === 200 && result.status === 'current' && result.release_id === release_id) return;
    if (response.status === 200 && result.status === 'prepared' && result.release_id === release_id
        && Number.isSafeInteger(result.lease_ms) && result.lease_ms >= 30_000) return;
    if (response.status !== 202 || result.status !== 'busy') throw new Error('API_HANDOVER_NOT_PREPARED');
    await pause(pollMs);
  }
  throw new Error('API_HANDOVER_BUSY');
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const started = Date.now();
  console.log(JSON.stringify({ status: 'image_prepared', at: new Date(started).toISOString() }));
  try {
    await prepareRelease({ token: readApiToken(), templateId: process.env.RAILSHOT_DESIRED_TEMPLATE_ID });
    console.log(JSON.stringify({ status: 'handover_prepared', waited_ms: Date.now() - started }));
  } catch {
    // Never print token, response body or connection details from the live service.
    console.error('API handover was not prepared; the existing API must remain running.');
    process.exitCode = 1;
  }
}
