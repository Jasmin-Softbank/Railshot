const API = 'https://api.github.com';
const namePattern = /^[a-z0-9-]{1,30}$/;

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export function createGithubClient(config, fetchImpl = fetch) {
  const { token, owner, repo, ref, tenant, workflow } = config;
  const base = `/repos/${owner}/${repo}`;
  const headers = {
    accept: 'application/vnd.github+json',
    authorization: `Bearer ${token}`,
    'x-github-api-version': '2026-03-10',
  };

  async function request(path, options = {}) {
    let response;
    try {
      response = await fetchImpl(`${API}${base}${path}`, {
        ...options,
        headers: { ...headers, ...(options.body ? { 'content-type': 'application/json' } : {}) },
        signal: AbortSignal.timeout(15000),
      });
    } catch {
      throw new ApiError(502, 'GitHub API에 연결하지 못했습니다.');
    }
    if (!response.ok) {
      if (response.status === 404) throw new ApiError(404, 'GitHub 리소스를 찾을 수 없습니다.');
      throw new ApiError(502, `GitHub API 요청이 실패했습니다 (${response.status}).`);
    }
    if (response.status === 204) return null;
    try { return await response.json(); } catch { throw new ApiError(502, 'GitHub API 응답을 읽지 못했습니다.'); }
  }

  async function dispatch(app) {
    if (!namePattern.test(app)) throw new ApiError(400, '앱 이름은 소문자·숫자·하이픈 1~30자여야 합니다.');
    const path = `apps/${tenant}/${app}`;
    let source;
    try {
      source = await request(`/contents/${path}?ref=${encodeURIComponent(ref)}`);
    } catch (error) {
      if (error.status === 404) throw new ApiError(404, `등록된 앱을 찾을 수 없습니다: ${app}`);
      throw error;
    }
    if (!Array.isArray(source)) throw new ApiError(409, '등록된 앱 경로가 디렉터리가 아닙니다.');
    const result = await request(`/actions/workflows/${encodeURIComponent(workflow)}/dispatches`, {
      method: 'POST',
      body: JSON.stringify({ ref, inputs: { tenant, app } }),
    });
    if (!result?.workflow_run_id) {
      return { runId: null, app, tenant, state: 'accepted_untracked', actionsUrl: `https://github.com/${owner}/${repo}/actions/workflows/${workflow}` };
    }
    return {
      runId: result.workflow_run_id,
      app,
      tenant,
      state: 'queued',
      actionsUrl: result.html_url || `https://github.com/${owner}/${repo}/actions/runs/${result.workflow_run_id}`,
    };
  }

  async function getRun(runId) {
    if (!/^\d+$/.test(String(runId))) throw new ApiError(400, '실행 ID가 잘못되었습니다.');
    const run = await request(`/actions/runs/${runId}`);
    if (run.event !== 'workflow_dispatch' || run.path?.split('@')[0] !== `.github/workflows/${workflow}`) {
      throw new ApiError(404, '해당 배포 실행을 찾을 수 없습니다.');
    }
    const result = await request(`/actions/runs/${runId}/jobs?filter=latest&per_page=100`);
    const jobs = (result.jobs || []).map((job) => ({
      name: job.name,
      status: job.status,
      conclusion: job.conclusion,
      steps: (job.steps || []).map((step) => ({ name: step.name, status: step.status, conclusion: step.conclusion })),
    }));
    const check = jobs.flatMap((job) => job.steps).find((step) => step.name?.startsWith('Verify the public URL'));
    const requiredJobsSucceeded = ['loop', 'release', 'gitops'].every((name) => jobs.find((job) => job.name === name)?.conclusion === 'success');
    return {
      runId: run.id,
      status: run.status,
      conclusion: run.conclusion,
      headSha: run.head_sha,
      actionsUrl: run.html_url,
      createdAt: run.created_at,
      updatedAt: run.updated_at,
      target: { environment: 'cloud', provider: 'aws' },
      requiredJobsSucceeded,
      publicHttpCheck: check?.conclusion === 'success' ? 'passed' : check?.conclusion === 'failure' ? 'failed' : check?.status === 'in_progress' ? 'running' : 'not_run',
      jobs,
      jobsTruncated: result.total_count > jobs.length,
    };
  }

  return { dispatch, getRun };
}
