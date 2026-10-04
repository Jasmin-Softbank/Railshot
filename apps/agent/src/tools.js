import { z } from 'zod';
import { createApiClient } from './api.js';

const resourceId = z.string().regex(/^[A-Za-z0-9._-]{1,128}$/);
const githubUrl = z.string().url().refine((value) => {
  const url = new URL(value);
  return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password && !url.port
    && !url.search && !url.hash && /^\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+(?:\.git)?\/?$/.test(url.pathname);
}, '공개 GitHub 저장소 기본 URL이 필요합니다.');

async function deploymentProgress(api, deploymentId, since) {
  const deployment = await api.deployment(deploymentId);
  if (deployment?.id !== deploymentId) throw new Error('요청한 배포와 상세 정보가 일치하지 않습니다.');
  const failed = ['failed', 'blocked', 'unknown'].includes(deployment.status);
  const [eventsResult, diagnosticResult] = await Promise.allSettled([
    api.deploymentEvents(deploymentId),
    failed ? api.deploymentDiagnostics(deploymentId) : Promise.resolve(null),
  ]);
  const fetchedEvents = eventsResult.status === 'fulfilled' && eventsResult.value?.deployment_id === deploymentId
    ? eventsResult.value : null;
  const runAttempt = fetchedEvents?.run_attempt || 0;
  const staleAttempt = fetchedEvents && runAttempt > 0 && (runAttempt < (deployment.ci?.producer_attempt || 0)
    || (since && runAttempt < since.run_attempt));
  const staleRevision = fetchedEvents?.agent_activity && since && runAttempt === since.run_attempt
    && fetchedEvents.agent_activity.id === since.activity_id
    && fetchedEvents.agent_activity.revision < (since.revision ?? -1);
  const staleEvents = staleAttempt || staleRevision;
  const events = staleEvents ? null : fetchedEvents;
  const activity = events?.agent_activity || null;
  const activityCursor = { run_attempt: events ? runAttempt : since?.run_attempt || 0,
    activity_id: events ? activity?.id || null : since?.activity_id || null,
    revision: events ? activity?.revision ?? null : since?.revision ?? null,
    observation_state: events ? activity?.observation?.state || events.state || null : since?.observation_state || null,
    deployment_status: deployment.status, deployment_stage: deployment.stage || null };
  const activityUpdated = Boolean(activityCursor && activity && (!since || activityCursor.run_attempt > since.run_attempt
    || activityCursor.activity_id !== since.activity_id || activityCursor.revision > (since.revision ?? -1)
    || (since.observation_state && activityCursor.observation_state !== since.observation_state)));
  const diagnostic = diagnosticResult.status === 'fulfilled' ? diagnosticResult.value : null;
  const binding = diagnostic?.binding;
  const matches = diagnostic?.deployment_id === deploymentId && binding?.app === deployment.app
    && binding?.target_id === deployment.target_id && binding?.source_commit === deployment.source_commit
    && String(binding?.run_id) === String(deployment.ci?.run_id);
  const ready = matches && diagnostic.state === 'ready';
  const logs = ready ? (diagnostic.logs || []).slice(0, 4).map((log) => ({
    process_id: log.process_id, text: String(log.text || '').slice(0, 4000),
    truncated: String(log.text || '').length > 4000,
  })) : [];
  return {
    deployment_id: deploymentId, status: deployment.status, stage: deployment.stage,
    error: deployment.error || null, url: deployment.url || null,
    ci: deployment.ci ? { run_id: deployment.ci.run_id, status: deployment.ci.status,
      steps: deployment.ci.steps || [], diagnostics: deployment.ci.diagnostics || null } : null,
    run_attempt: events?.run_attempt || since?.run_attempt || null,
    events_status: events?.status || null,
    events_state: staleEvents ? 'stale_response' : events?.state || (!events ? 'unavailable' : null),
    progress: events?.progress || null,
    agent_activity: activity,
    activity_cursor: activityCursor,
    deployment_updated: !since || since.deployment_status !== deployment.status
      || since.deployment_stage !== (deployment.stage || null),
    run_attempt_changed: Boolean(since && events && runAttempt > since.run_attempt),
    agent_activity_updated: activityUpdated,
    diagnostics: failed ? { state: ready ? 'ready' : matches ? diagnostic.state : 'unavailable',
      failure: ready ? diagnostic.failure : null, logs,
      logs_truncated: Boolean(ready && (diagnostic.logs?.length > logs.length || logs.some((log) => log.truncated))) } : null,
    poll_after_ms: deployment.status === 'succeeded' || (failed && events?.status === 'completed')
      ? null : Math.max(5000, events?.progress?.poll_after_ms || 15000),
  };
}

export const tools = {
  list_options: {
    title: '배포 환경 선택지 조회',
    description: '사용 가능한 클라우드 및 온프레미스 배포 환경과 미지원 사유를 조회합니다.',
    schema: z.object({}).strict(), readOnly: true,
    run: (api) => api.options(),
  },
  list_targets: {
    title: '등록된 배포 대상 조회',
    description: '등록된 대상과 CI 제출 및 앱 배포 가능 여부를 조회합니다.',
    schema: z.object({}).strict(), readOnly: true,
    run: (api) => api.targets(),
  },
  get_deployment: {
    title: '배포 상태 조회',
    description: '배포 ID로 실제 단계, 상태, 오류와 검증된 URL을 조회합니다.',
    schema: z.object({ deployment_id: resourceId }).strict(), readOnly: true,
    run: (api, { deployment_id }) => api.deployment(deployment_id),
  },
  get_deployment_progress: {
    title: '배포 진행 및 실패 원인 조회',
    description: '배포 상태와 AI 장애 조치(agent_activity: 작업 요약·현재 작업·변경 파일·검증·이전 시도·관측 상태), 실패 원인 및 CI 로그를 조회합니다. poll_after_ms 뒤 activity_cursor를 since로 전달해 반복 조회하고 새 내용만 대화에 알리세요. 배포 성공 여부는 별도로 확인하세요.',
    schema: z.object({ deployment_id: resourceId, since: z.object({
      run_attempt: z.number().int().nonnegative(), activity_id: z.string().max(256).nullable(),
      revision: z.number().int().nonnegative().nullable(), observation_state: z.string().max(32).nullable(),
      deployment_status: z.string().max(32).optional(), deployment_stage: z.string().max(32).nullable().optional(),
    }).strict().optional() }).strict(), readOnly: true,
    run: (api, { deployment_id, since }) => deploymentProgress(api, deployment_id, since),
  },
  get_build: {
    title: '빌드 상태 조회',
    description: '등록된 빌드 ID로 CI와 이미지 게시 상태를 조회합니다. 게시만으로 앱 배포가 완료되지는 않습니다.',
    schema: z.object({ build_id: resourceId }).strict(), readOnly: true,
    run: (api, { build_id }) => api.build(build_id),
  },
  deploy_repository: {
    title: '공개 GitHub 저장소 배포 요청',
    description: '명시된 앱·대상에 공개 GitHub 저장소 URL로만 배포합니다. 로컬 폴더·파일·ZIP 업로드는 지원하지 않습니다. 호출 전 사용자의 별도 승인이 필요합니다. 접수 결과는 배포 완료가 아닙니다.',
    schema: z.object({
      repository_url: githubUrl,
      app: z.string().regex(/^[a-z](?:[a-z0-9-]{1,28}[a-z0-9])$/),
      target_id: resourceId,
      idempotency_key: z.string().regex(/^[A-Za-z0-9._-]{1,128}$/),
    }).strict(), readOnly: false,
    run: (api, args) => api.deploy(args),
  },
};

export function createToolRunner(api = createApiClient()) {
  return async function call(name, input) {
    const tool = tools[name];
    if (!tool) throw new Error('지원하지 않는 도구입니다.');
    return tool.run(api, tool.schema.parse(input));
  };
}
