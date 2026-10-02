import { createHash } from 'node:crypto';
import { inspectArchive, validateFiles, archiveLimits } from './archive.js';
import { APP_NAME, APP_NAME_MESSAGE, TENANT_NAME, TARGET_ID, SOURCE_COMMIT } from './contract.js';
import { readPublished } from './published.js';

const API = 'https://api.github.com';

export class ServiceError extends Error {
  constructor(message, status = 500) { super(message); this.status = status; }
}

export function createDeploymentService(config, fetchImpl = fetch) {
  const { token, owner = 'Jasmin-Softbank', repo = 'railshot-apps', ref = 'main', tenant = 'demo', workflow = 'railshot-deploy.yml', targetId } = config;
  if (!token) throw new Error('GITHUB_TOKEN을 설정하세요.');
  if (typeof tenant !== 'string' || !TENANT_NAME.test(tenant)) throw new Error('JASMIN_TENANT가 잘못되었습니다.');
  if (typeof targetId !== 'string' || !TARGET_ID.test(targetId)) throw new Error('RAILSHOT_TARGET_ID에 운영자가 준비할 대상 ID를 설정하세요.');
  const repoPath = `/repos/${owner}/${repo}`;

  async function request(path, options = {}) {
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
    return response.status === 204 ? {} : response.json();
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

  async function dispatch(app, sourceCommit, changes, source) {
    const dispatched = await request(`${repoPath}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`, {
      method: 'POST', body: JSON.stringify({ ref, inputs: { tenant, app, source_commit: sourceCommit, target_id: targetId } }),
    });
    if (!dispatched.workflow_run_id) throw new ServiceError('앱은 등록됐지만 Actions 실행 ID를 받지 못했습니다. GitHub Actions를 확인하세요.', 502);
    return {
      run_id: dispatched.workflow_run_id, tenant, app, source_commit: sourceCommit, target_id: targetId, state: 'queued', changes, ...(source ? { source } : {}),
      actions_url: dispatched.html_url || `https://github.com/${owner}/${repo}/actions/runs/${dispatched.workflow_run_id}`,
    };
  }

  async function deploy({ app, files, source, target_id = targetId }) {
    if (typeof app !== 'string' || !APP_NAME.test(app)) throw new ServiceError(APP_NAME_MESSAGE, 400);
    if (target_id !== targetId) throw new ServiceError('이 API에 설정된 배포 대상과 일치하지 않습니다.', 400);
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
    return dispatch(app, sourceCommit, changes, source);
  }

  async function validateRegistered({ app, target_id = targetId }) {
    if (typeof app !== 'string' || !APP_NAME.test(app)) throw new ServiceError(APP_NAME_MESSAGE, 400);
    if (target_id !== targetId) throw new ServiceError('이 API에 설정된 배포 대상과 일치하지 않습니다.', 400);
    const branch = await request(`${repoPath}/git/ref/heads/${encodeURIComponent(ref)}`);
    const sourceCommit = branch.object.sha;
    if (!SOURCE_COMMIT.test(sourceCommit)) throw new ServiceError('소스 commit SHA를 확인하지 못했습니다.', 502);
    const base = await request(`${repoPath}/git/commits/${sourceCommit}`);
    const existing = await findAppTree(base.tree.sha, app);
    if (!existing?.length) throw new ServiceError(`등록된 앱을 찾을 수 없습니다: ${app}`, 404);
    return { sourceCommit, fileCount: existing.length };
  }

  async function redeploy(input) {
    const { sourceCommit, fileCount } = await validateRegistered(input);
    return dispatch(input.app, sourceCommit, { added: 0, updated: 0, deleted: 0, unchanged: fileCount });
  }

  async function status(runId) {
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
        observed_attempt: job?.observed_attempt || null,
        actions_steps: (Array.isArray(job?.steps) ? job.steps : []).map((step) => ({
          name: step.name, status: step.status, conclusion: step.conclusion,
        })) };
    });
    const completed = run.status === 'completed';
    const conclusion = completed && run.conclusion === 'success' && steps.some((step) => step.conclusion !== 'success')
      ? 'failure' : run.conclusion;
    let publication = null;
    let artifactError = null;
    if (completed && conclusion === 'success') {
      try { publication = await published(runId, observed.get('release').observed_attempt, run.head_sha); }
      catch (error) { artifactError = error.message || '게시 산출물을 확인하지 못했습니다.'; }
    }
    const state = publication ? 'published' : artifactError ? 'publication_unverified'
      : completed ? 'failed' : run.status === 'in_progress' ? 'running' : 'queued';
    return { run_id: Number(runId), app: publication?.app || null, tenant,
      source_commit: publication?.source_commit || run.head_sha || null, target_id: targetId,
      status: run.status, conclusion, state, actions_url: run.html_url, steps, url: null,
      publication, message: publication ? '검증한 이미지가 게시되었습니다. 대상 앱 적용과 외부 URL 확인은 아직 수행하지 않았습니다.'
        : artifactError || (state === 'failed' ? 'CI가 완료되지 않았습니다. GitHub Actions 로그를 확인하세요.' : null) };
  }

  async function published(runId, attempt, headSha, includeFiles = false) {
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
    const receipt = readPublished(files, { runId, attempt, headSha, targetId, tenant });
    const publication = { ...receipt, artifact_id: artifact.id, artifact_name: name };
    return includeFiles ? { publication, files } : publication;
  }

  async function publishedFiles(publication) {
    if (!publication || !Number.isSafeInteger(publication.run_id) || !Number.isSafeInteger(publication.producer_attempt) || !SOURCE_COMMIT.test(publication.source_commit || '')) throw new ServiceError('게시 참조가 잘못되었습니다.', 502);
    const verified = await published(String(publication.run_id), publication.producer_attempt, publication.source_commit, true);
    if (JSON.stringify(verified.publication) !== JSON.stringify(publication)) throw new ServiceError('게시 참조가 변경되었습니다.', 502);
    return verified.files;
  }

  return { deploy, redeploy, validateRegistered, status, publishedFiles, targetId };
}
