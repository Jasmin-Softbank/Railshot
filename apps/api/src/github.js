import { createHash } from 'node:crypto';
import { setTimeout as pause } from 'node:timers/promises';
import { inspectArchive, validateFiles, archiveLimits } from './archive.js';
import { APP_NAME, APP_NAME_MESSAGE, TENANT_NAME, TARGET_ID, SOURCE_COMMIT } from './contract.js';
import { readPublished } from './published.js';
import { AgentEventError, agentEventCheckName, agentEventLimits, validateEventBinding, validateEventRun, readAgentEventCheck, emptyAgentEvents } from './agent-events.js';

const API = 'https://api.github.com';
const diagnosticLimit = 2 * 1024 * 1024;
const stepNames = new Set(['Set up job', 'Complete job', 'Validate trusted platform bindings', 'Validate inputs',
  'Baseline gates, then SDK repair only when eligible', 'Export the exact gate-tested images',
  'Set registry credential path', 'Match the CI containerd image store', 'Log in to GHCR',
  "Recover the same workflow run's publication history", 'Verify bundle and publish tested images',
  'Preserve publication journal without credentials', 'Bind publication to the source and target', 'Remove ephemeral registry credentials']);
const errorCodes = new Set(('INTAKE_REJECTED STATE_USAGE_INVALID STATE_BINDING_MISMATCH STATE_INFLIGHT_UNCERTAIN '
  + 'STATE_EVIDENCE_MISMATCH STATE_WRITER_CONFLICT STATE_STORAGE_FAILED STEP_TIMEOUT STEP_START_FAILED STEP_OUTPUT_INVALID '
  + 'SDK_CONFIG_INVALID SDK_POLICY_DENIED SDK_SANDBOX_UNAVAILABLE SDK_RESUME_UNSUPPORTED SDK_OUTCOME_UNKNOWN '
  + 'SDK_EXECUTION_FAILED SDK_OUTPUT_INVALID SDK_PATCH_REJECTED OBSERVATION_WRITE_FAILED GATE_CONFIG_INVALID '
  + 'GATE_EXECUTION_FAILED GATE_CHECK_FAILED GATE_EVIDENCE_MISMATCH GATE_ENVIRONMENT_UNAVAILABLE INTERNAL_ERROR').split(' '));
const diagnosticPhases = new Set(['intake', 'config', 'execution', 'observation', 'complete', 'agent', 'agent.error', 'agent.checkpoint',
  'Q', 'Q.discovery', 'Q.build', 'Q.test', 'Q.typecheck', 'Q.lint', 'L0', 'L1', 'L2', 'L3', 'L4', 'L5']);
const packagingFiles = new Set(['Dockerfile', '.dockerignore', '.railshot/railshot.yaml', '.jasmin/jasmin.yaml']);
function jobTasks(job) {
  if (!Array.isArray(job?.steps)) return [];
  return job.steps.slice(0, 50).filter((step) => Number.isInteger(step?.number) && step.number > 0 && step.number <= 100).map((step) => ({
    number: step.number, name: stepNames.has(step.name) ? step.name : `단계 ${step.number}`,
    status: ['queued', 'in_progress', 'completed', 'waiting', 'pending'].includes(step.status) ? step.status : 'unknown',
    conclusion: ['success', 'failure', 'skipped', 'cancelled', 'timed_out', 'neutral', 'action_required', 'stale'].includes(step.conclusion) ? step.conclusion : null,
  }));
}
function safeDiagnostics(evidence, attempt, artifactId) {
  const outcomes = ['FAIL', 'BLOCKED', 'UNKNOWN', 'NOT_RUN', 'INCOMPLETE'];
  if (!evidence || evidence.passed !== false || !outcomes.includes(evidence.status)
      || !Array.isArray(evidence.attempts) || evidence.attempts.length > 4
      || evidence.attempts.some((row, index) => !row || row.attempt !== index)
      || !Number.isInteger(evidence.agent_attempts) || evidence.agent_attempts < 0 || evidence.agent_attempts > 3
      || evidence.agent_attempts !== evidence.attempts.filter((row) => row.agent_invoked === true).length
      || evidence.error && evidence.error.outcome !== evidence.status) throw new Error('Invalid CI diagnostics');
  const code = errorCodes.has(evidence.error?.code) ? evidence.error.code : 'CI_FAILED';
  const phase = diagnosticPhases.has(evidence.error?.phase) ? evidence.error.phase : null;
  const repairScope = ['packaging', 'source'].includes(evidence.repair_scope) ? evidence.repair_scope : null;
  const changed = new Set(evidence.attempts.flatMap((row) => Array.isArray(row.written) ? row.written : []).filter((path) => {
    if (typeof path !== 'string' || path.length > 240 || /[\x00-\x1f\x7f]/.test(path)) return false;
    try { validateFiles([{ path, content: Buffer.alloc(0) }]); return true; } catch { return false; }
  }));
  const noTests = evidence.result === 'blocked: NO_TESTS' && code === 'GATE_CONFIG_INVALID' && phase === 'Q.discovery' && evidence.status === 'BLOCKED';
  let guidance = '실패 원인을 수정한 뒤 다시 요청하세요. 실행 결과가 불확실하면 운영자가 먼저 확인해야 합니다.';
  if (noTests) {
    guidance = '원본 프로젝트의 테스트와 테스트 실행 명령을 확인한 뒤 다시 배포하세요.';
    if (repairScope === 'packaging') guidance = '원본 프로젝트에 실행 가능한 테스트와 테스트 명령을 추가한 뒤 다시 배포하세요. 자동 패키징 수정은 테스트를 만들거나 품질 검사를 생략하지 않습니다.';
    if (repairScope === 'source') guidance = `${evidence.agent_attempts ? '허용된 범위에서 소스·테스트 자동 수정을 시도했지만 실행할 테스트를 확인하지 못했습니다.' : '소스·테스트 자동 수정이 허용된 실행이지만 수정 시도는 기록되지 않았습니다.'} 남은 테스트 구성과 실행 명령을 확인하세요. 품질 검사는 생략하지 않습니다.`;
  }
  return { state: 'ready', run_attempt: attempt, artifact_id: artifactId, reason: noTests ? 'NO_TESTS' : 'CI_FAILED', code, phase,
    outcome: evidence.status,
    message: noTests ? '실행할 테스트를 찾지 못해 품질 검사가 중단되었습니다.' : 'CI 검사가 완료되지 않았습니다. 실패한 단계와 GitHub Actions 기록을 확인하세요.',
    guidance, repair_scope: repairScope, agent_attempts: evidence.agent_attempts,
    changed_files: [...changed].filter((name) => packagingFiles.has(name)), changed_file_count: changed.size,
  };
}

export class ServiceError extends Error {
  constructor(message, status = 500) { super(message); this.status = status; }
}

export function createDeploymentService(config, fetchImpl = fetch) {
  const { token, owner = 'Jasmin-Softbank', repo = 'railshot-apps', ref = 'main', tenant = 'demo', workflow = 'railshot-deploy.yml', targetId } = config;
  if (!token) throw new Error('GITHUB_TOKEN을 설정하세요.');
  if (typeof tenant !== 'string' || !TENANT_NAME.test(tenant)) throw new Error('RAILSHOT_TENANT가 잘못되었습니다.');
  if (typeof targetId !== 'string' || !TARGET_ID.test(targetId)) throw new Error('RAILSHOT_TARGET_ID에 운영자가 준비할 대상 ID를 설정하세요.');
  if (config.targetIds !== undefined && !Array.isArray(config.targetIds)) throw new Error('등록된 CI target 목록이 잘못되었습니다.');
  const targetIds = new Set(config.targetIds || [targetId]);
  if (!targetIds.has(targetId) || targetIds.size > 100 || [...targetIds].some((id) => typeof id !== 'string' || !TARGET_ID.test(id)))
    throw new Error('등록된 CI target 목록이 잘못되었습니다.');
  const permittedTarget = (id) => { if (!targetIds.has(id)) throw new ServiceError('등록된 배포 대상과 일치하지 않습니다.', 400); };
  // Called only by the trusted product worker after durable environment registration.
  function allowTarget(id) {
    if (typeof id !== 'string' || !TARGET_ID.test(id)) throw new ServiceError('등록된 CI target ID가 잘못되었습니다.', 400);
    targetIds.add(id);
  }
  const repoPath = `/repos/${owner}/${repo}`;

  async function request(path, options = {}, maxBytes = null) {
    const response = await fetchImpl(`${API}${path}`, {
      signal: AbortSignal.timeout(30_000), ...options,
      headers: {
        accept: 'application/vnd.github+json',
        authorization: `Bearer ${token}`,
        'x-github-api-version': '2026-03-10',
        ...(options.body ? { 'content-type': 'application/json' } : {}),
        ...options.headers,
      },
    });
    if (!response.ok) {
      throw new ServiceError(`GitHub API 요청 실패 (${response.status}).`, [404, 409].includes(response.status) ? response.status : 502);
    }
    if (response.status === 204) return {};
    if (maxBytes === 0) {
      if (response.status !== 202) throw new ServiceError('취소 접수를 확인하지 못했습니다.', 502);
      return {};
    }
    if (maxBytes === null) return response.json();
    if (Number(response.headers.get('content-length')) > maxBytes) {
      await response.body?.cancel(); throw new AgentEventError('too_large');
    }
    const chunks = []; let bytes = 0;
    for await (const chunk of response.body) {
      bytes += chunk.byteLength;
      if (bytes > maxBytes) throw new AgentEventError('too_large');
      chunks.push(Buffer.from(chunk));
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  }

  // Small process-local cache; the product checks ownership before reaching it.
  // Always read the run first so a rerun cannot reuse a previous attempt's cache.
  const eventCache = new Map();
  async function events(runId, binding) {
    const identity = { ...binding, runId: String(runId), tenant, owner, repo, ref, workflow };
    try {
      validateEventBinding(identity.runId, identity);
      if (!targetIds.has(identity.target_id)) throw new AgentEventError('binding_mismatch');
      const options = { redirect: 'error', signal: AbortSignal.timeout(15_000) };
      const read = (path) => request(path, options, agentEventLimits.responseBytes);
      const runPath = `${repoPath}/actions/runs/${identity.runId}`;
      const run = await read(runPath);
      identity.attempt = validateEventRun(run, identity);
      const key = JSON.stringify([owner, repo, ref, workflow, identity.runId, identity.attempt, identity.source_commit,
        identity.app, tenant, identity.target_id, run.status]);
      let cached = eventCache.get(key);
      if (!cached || cached.expires <= Date.now()) {
        const result = (async () => {
          const checks = [];
          let complete = false, total = null;
          for (let page = 1; page <= 5; page++) {
            const listing = await read(`${repoPath}/commits/${identity.source_commit}/check-runs?check_name=${encodeURIComponent(agentEventCheckName)}&filter=all&app_id=15368&per_page=20&page=${page}`);
            if (!Number.isSafeInteger(listing.total_count) || listing.total_count < 0 || listing.total_count > 100
                || !Array.isArray(listing.check_runs) || listing.check_runs.length > 20
                || total !== null && total !== listing.total_count) throw new AgentEventError('producer_mismatch');
            total = listing.total_count; checks.push(...listing.check_runs);
            if (checks.length === total) { complete = true; break; }
            if (listing.check_runs.length < 20 || checks.length > total) throw new AgentEventError('producer_mismatch');
          }
          if (!complete || new Set(checks.map((check) => check.id)).size !== checks.length) throw new AgentEventError('producer_mismatch');
          const matches = checks.filter((check) => check.external_id === `railshot-events:${identity.runId}:${identity.attempt}`);
          if (!matches.length) return null;
          if (matches.length !== 1) throw new AgentEventError('producer_mismatch');
          return readAgentEventCheck(matches[0], identity);
        })();
        cached = { expires: Date.now() + agentEventLimits.cacheMs, result };
        eventCache.delete(key);
        if (eventCache.size >= 100) eventCache.delete(eventCache.keys().next().value);
        eventCache.set(key, cached);
        result.catch(() => { if (eventCache.get(key) === cached) eventCache.delete(key); });
      }
      const envelope = await cached.result;
      if (validateEventRun(await read(runPath), identity) !== identity.attempt) throw new AgentEventError('attempt_changed');
      if (!envelope) return emptyAgentEvents(identity, 'not_started', 'not_available');
      return { ...structuredClone(envelope), state: envelope.items.length ? envelope.status === 'completed' ? 'complete' : 'live' : 'no_data',
        reason: null, checked_at: new Date().toISOString(), stale: envelope.status === 'running' && Date.now() - Date.parse(envelope.updated_at) > 60_000,
        next_marker: null };
    } catch (error) {
      return emptyAgentEvents(identity, 'unavailable', error instanceof AgentEventError ? error.reason : 'upstream_unavailable');
    }
  }

  async function findAppTree(rootSha, app) {
    let sha = rootSha;
    for (const segment of ['apps', tenant, app]) {
      const parent = await request(`${repoPath}/git/trees/${sha}`);
      const entry = parent.tree.find((item) => item.path === segment);
      if (!entry) return null;
      if (entry.type !== 'tree') throw new ServiceError(`앱 경로가 디렉터리가 아닙니다: ${segment}`, 409);
      sha = entry.sha;
    }
    const tree = await request(`${repoPath}/git/trees/${sha}?recursive=1`);
    if (tree.truncated) throw new ServiceError('기존 앱 파일 목록이 너무 커서 안전하게 갱신할 수 없습니다.', 409);
    return tree.tree.filter((item) => item.type !== 'tree');
  }

  function blobSha(content) {
    return createHash('sha1').update(`blob ${content.length}\0`).update(content).digest('hex');
  }

  async function deploy({ app, files, source, target_id = targetId }) {
    if (typeof app !== 'string' || !APP_NAME.test(app)) throw new ServiceError(APP_NAME_MESSAGE, 400);
    permittedTarget(target_id);
    const acceptedFiles = validateFiles(files);
    const prefix = `apps/${tenant}/${app}`;
    const branch = await request(`${repoPath}/git/ref/heads/${encodeURIComponent(ref)}`);
    const parent = branch.object.sha;
    if (!SOURCE_COMMIT.test(parent)) throw new ServiceError('소스 commit SHA를 확인하지 못했습니다.', 502);
    let sourceCommit = parent;
    const base = await request(`${repoPath}/git/commits/${parent}`);
    const existing = await findAppTree(base.tree.sha, app);
    const previous = new Map(existing?.map((item) => [item.path, item]) || []);
    const changes = { added: 0, updated: 0, deleted: 0, unchanged: 0 };
    const treeEntries = [];
    for (const file of acceptedFiles) {
      const before = previous.get(file.path);
      let sha = blobSha(file.content);
      if (!before) changes.added++;
      else if (before.sha !== sha || before.mode !== '100644' || before.type !== 'blob') changes.updated++;
      else changes.unchanged++;
      if (before?.sha !== sha || before.type !== 'blob') {
        const blob = await request(`${repoPath}/git/blobs`, {
          method: 'POST',
          body: JSON.stringify({ content: file.content.toString('base64'), encoding: 'base64' }),
        });
        sha = blob.sha;
      }
      treeEntries.push({ path: file.path, mode: '100644', type: 'blob', sha });
    }
    const incomingPaths = new Set(acceptedFiles.map((file) => file.path));
    changes.deleted = [...previous.keys()].filter((path) => !incomingPaths.has(path)).length;
    if (changes.added || changes.updated || changes.deleted) {
      // Replace only this app's tree; absent paths disappear, while other apps stay on base_tree.
      const appTree = await request(`${repoPath}/git/trees`, {
        method: 'POST', body: JSON.stringify({ tree: treeEntries }),
      });
      const tree = await request(`${repoPath}/git/trees`, {
        method: 'POST', body: JSON.stringify({ base_tree: base.tree.sha, tree: [
          { path: prefix, mode: '040000', type: 'tree', sha: appTree.sha },
        ] }),
      });
      const commit = await request(`${repoPath}/git/commits`, {
        method: 'POST',
        body: JSON.stringify({ message: `${existing ? 'fix: update' : 'feat: add'} ${tenant}/${app} via entrypoints PoC${source?.type === 'github' ? `\n\nSource: ${source.repository}@${source.sha}` : ''}`, tree: tree.sha, parents: [parent] }),
      });
      if (!SOURCE_COMMIT.test(commit.sha)) throw new ServiceError('등록된 commit SHA를 확인하지 못했습니다.', 502);
      sourceCommit = commit.sha;
      await request(`${repoPath}/git/refs/heads/${encodeURIComponent(ref)}`, {
        method: 'PATCH', body: JSON.stringify({ sha: commit.sha, force: false }),
      });
    }
    const dispatched = await request(`${repoPath}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`, {
      method: 'POST', body: JSON.stringify({ ref, inputs: { tenant, app, source_commit: sourceCommit, target_id } }),
    });
    if (!dispatched.workflow_run_id) {
      throw new ServiceError('앱은 등록됐지만 Actions 실행 ID를 받지 못했습니다. GitHub Actions를 확인하세요.', 502);
    }
    return {
      run_id: dispatched.workflow_run_id, tenant, app, source_commit: sourceCommit, target_id, state: 'queued', changes, ...(source ? { source } : {}),
      actions_url: dispatched.html_url || `https://github.com/${owner}/${repo}/actions/runs/${dispatched.workflow_run_id}`,
    };
  }

  async function status(runId, expectedTargetId = targetId) {
    permittedTarget(expectedTargetId);
    if (!/^\d+$/.test(String(runId))) throw new ServiceError('run_id가 잘못되었습니다.', 400);
    const run = await request(`${repoPath}/actions/runs/${runId}`);
    if (run.path !== `.github/workflows/${workflow}`) throw new ServiceError('해당 실행은 등록된 CI 워크플로가 아닙니다.', 404);
    if (!Number.isInteger(run.run_attempt) || run.run_attempt < 1 || run.run_attempt > 100) throw new ServiceError('지원 범위에서 실행 attempt를 확인하지 못했습니다.', 502);
    const observed = new Map();
    // GitHub may copy reused jobs into a later attempt; this identifies the observed snapshot.
    for (let attempt = run.run_attempt; attempt >= 1 && observed.size < 2; attempt--) {
      const page = await request(`${repoPath}/actions/runs/${runId}/attempts/${attempt}/jobs?per_page=100`);
      if (!Array.isArray(page.jobs) || page.total_count > 100) throw new ServiceError('CI job 목록이 불완전합니다.', 502);
      for (const key of ['loop', 'release']) {
        const jobs = page.jobs.filter((item) => item.name === key);
        if (jobs.length > 1) throw new ServiceError('CI job 식별자가 중복됩니다.', 502);
        if (!observed.has(key) && jobs[0]) observed.set(key, { ...jobs[0], observed_attempt: attempt });
      }
      if (run.status !== 'completed') break;
    }
    const steps = ['loop', 'release'].map((key) => {
      const job = observed.get(key);
      return { key, status: job?.status || (run.status === 'completed' ? 'completed' : 'queued'),
        conclusion: job?.conclusion || (run.status === 'completed' ? 'skipped' : null),
        observed_attempt: job?.observed_attempt || null, tasks: jobTasks(job) };
    });
    const completed = run.status === 'completed';
    const conclusion = completed && run.conclusion === 'success' && steps.some((step) => step.conclusion !== 'success')
      ? 'failure' : run.conclusion;
    let publication = null;
    let artifactError = null;
    if (completed && conclusion === 'success') {
      try { publication = await published(runId, observed.get('release').observed_attempt, run.head_sha, false, expectedTargetId); }
      catch { artifactError = '게시 산출물의 식별자·무결성·계약을 확인하지 못했습니다. GitHub Actions 기록을 확인하세요.'; }
    }
    const state = publication ? 'published' : artifactError ? 'publication_unverified'
      : completed ? 'failed' : run.status === 'in_progress' ? 'running' : 'queued';
    let diagnostics = null;
    if (state === 'failed' && observed.get('loop')?.conclusion === 'failure') {
      diagnostics = { state: 'unavailable', run_attempt: run.run_attempt, reason: 'CI_FAILED',
        message: '현재 CI 실행의 진단 자료를 확인하지 못했습니다. GitHub Actions 기록을 확인하세요.' };
      if (observed.get('loop').observed_attempt === run.run_attempt) {
        try { diagnostics = await failedDiagnostics(runId, run.run_attempt, run.head_sha); }
        catch { /* A missing or untrusted diagnostic must not change the failed CI result. */ }
      }
    }
    return { run_id: Number(runId), app: publication?.app || null, tenant,
      source_commit: publication?.source_commit || run.head_sha || null, target_id: expectedTargetId,
      status: run.status, conclusion, state, actions_url: run.html_url, steps, url: null,
      publication, diagnostics, message: publication ? '검증한 이미지가 게시되었습니다. 대상 앱 적용과 외부 URL 확인은 아직 수행하지 않았습니다.'
        : artifactError || (state === 'failed' ? 'CI가 완료되지 않았습니다. GitHub Actions 로그를 확인하세요.' : null) };
  }

  async function failedDiagnostics(runId, attempt, headSha) {
    if (!SOURCE_COMMIT.test(headSha || '')) throw new Error('Invalid diagnostic source');
    const name = `loop-${attempt}`;
    const list = await request(`${repoPath}/actions/runs/${runId}/artifacts?name=${name}&per_page=100`);
    if (!Number.isSafeInteger(list.total_count) || list.total_count < 1 || list.total_count > 100
        || !Array.isArray(list.artifacts) || list.artifacts.length > list.total_count) throw new Error('Incomplete diagnostic artifacts');
    const matches = list.artifacts.filter((item) => item.name === name), artifact = matches[0];
    if (matches.length !== 1 || artifact.expired !== false || !Number.isSafeInteger(artifact.id) || artifact.id < 1
        || artifact.workflow_run?.id !== Number(runId) || artifact.workflow_run?.head_sha !== headSha
        || !Number.isSafeInteger(artifact.size_in_bytes) || artifact.size_in_bytes < 1 || artifact.size_in_bytes > diagnosticLimit) throw new Error('Invalid diagnostic artifact binding');
    const response = await fetchImpl(`${API}${repoPath}/actions/artifacts/${artifact.id}/zip`, {
      signal: AbortSignal.timeout(30_000), headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json', 'x-github-api-version': '2026-03-10' },
    });
    if (!response.ok) throw new Error('Diagnostic download failed');
    const chunks = []; let size = 0;
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > diagnosticLimit) throw new Error('Diagnostic download too large');
      chunks.push(chunk);
    }
    const files = await inspectArchive(Buffer.concat(chunks));
    if (files.length > 20 || files.reduce((sum, file) => sum + file.content.length, 0) > diagnosticLimit) throw new Error('Diagnostic content too large');
    const evidence = files.find((file) => file.path === 'evidence.json');
    if (!evidence || evidence.content.length > 262144) throw new Error('Diagnostic evidence unavailable');
    return safeDiagnostics(JSON.parse(evidence.content.toString('utf8')), attempt, artifact.id);
  }

  async function published(runId, attempt, headSha, includeFiles = false, expectedTargetId = targetId) {
    permittedTarget(expectedTargetId);
    const name = `published-${attempt}`;
    const list = await request(`${repoPath}/actions/runs/${runId}/artifacts?name=${name}&per_page=100`);
    const matches = list.artifacts?.filter((item) => item.name === name) || [];
    if (list.total_count > 100 || matches.length !== 1 || matches[0].expired) throw new ServiceError('해당 producer attempt의 게시 산출물이 없거나 중복·만료되었습니다.', 502);
    const artifact = matches[0];
    if (!Number.isSafeInteger(artifact.id) || artifact.id < 1 || artifact.workflow_run?.id !== Number(runId) || artifact.workflow_run?.head_sha !== headSha) throw new ServiceError('게시 artifact의 run/source 식별자가 다릅니다.', 502);
    if (artifact.size_in_bytes > archiveLimits.maxBytes) throw new ServiceError('게시 artifact가 허용 크기를 초과했습니다.', 502);
    // Build the GitHub URL from the verified ID; do not send the token to an artifact-supplied URL.
    const response = await fetchImpl(`${API}${repoPath}/actions/artifacts/${artifact.id}/zip`, {
      signal: AbortSignal.timeout(30_000), headers: { authorization: `Bearer ${token}`, accept: 'application/vnd.github+json', 'x-github-api-version': '2026-03-10' },
    });
    if (!response.ok) throw new ServiceError('게시 artifact를 다운로드하지 못했습니다.', 502);
    const files = await inspectArchive(Buffer.from(await response.arrayBuffer()));
    const receipt = readPublished(files, { runId, attempt, headSha, targetId: expectedTargetId, tenant });
    const publication = { ...receipt, artifact_id: artifact.id, artifact_name: name };
    return includeFiles ? { publication, files } : publication;
  }

  async function publishedFiles(publication) {
    if (!publication || !Number.isSafeInteger(publication.run_id) || !Number.isSafeInteger(publication.producer_attempt) || !SOURCE_COMMIT.test(publication.source_commit || '')) throw new ServiceError('게시 참조가 잘못되었습니다.', 502);
    const verified = await published(String(publication.run_id), publication.producer_attempt, publication.source_commit, true, publication.target_id);
    if (JSON.stringify(verified.publication) !== JSON.stringify(publication)) throw new ServiceError('게시 참조가 변경되었습니다.', 502);
    return verified.files;
  }

  async function cancel(runId, binding, { signal } = {}) {
    permittedTarget(binding.target_id);
    if (!/^\d+$/.test(String(runId)) || !SOURCE_COMMIT.test(binding.source_commit || '')) throw new ServiceError('취소할 실행 식별자가 잘못되었습니다.', 400);
    const path = `${repoPath}/actions/runs/${runId}`;
    const identity = { runId: String(runId), source_commit: binding.source_commit, owner, repo, ref, workflow };
    const before = await request(path, { redirect: 'error' });
    const attempt = validateEventRun(before, identity);
    // Product dispatch creates a new run at attempt 1, including older bindings.
    if (attempt !== (binding.run_attempt ?? 1)) throw new ServiceError('접수한 실행 attempt가 달라 취소할 수 없습니다.', 409);
    if (before.status === 'completed') return { status: 'completed', run_id: String(runId), attempt };
    // Exactly one mutation; a lost response is never retried or force-cancelled.
    await request(path + '/cancel', { method: 'POST', redirect: 'error' }, 0);
    const deadline = Date.now() + 120000;
    while (!signal?.aborted && Date.now() < deadline) {
      const observed = await request(path, { redirect: 'error' });
      if (validateEventRun(observed, identity) !== attempt) throw new ServiceError('실행 attempt가 바뀌어 취소 결과를 확정할 수 없습니다.', 502);
      if (observed.status === 'completed') return { status: 'completed', run_id: String(runId), attempt };
      await pause(1000, undefined, { signal });
    }
    throw new ServiceError('CI 실행 종료를 확인하지 못했습니다.', 502);
  }

  return { deploy, status, cancel, events, publishedFiles, allowTarget, targetId,
    identity: Object.freeze({ tenant, sourceRepository: `${owner}/${repo}` }),
    get targetIds() { return Object.freeze([...targetIds]); } };
}
