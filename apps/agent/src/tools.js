import { z } from 'zod';
import { createApiClient } from './api.js';

const resourceId = z.string().regex(/^[A-Za-z0-9._-]{1,128}$/);
const githubUrl = z.string().url().refine((value) => {
  const url = new URL(value);
  return url.protocol === 'https:' && url.hostname === 'github.com' && !url.username && !url.password && !url.port
    && !url.search && !url.hash && /^\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+(?:\.git)?\/?$/.test(url.pathname);
}, '공개 GitHub 저장소 기본 URL이 필요합니다.');

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
  get_build: {
    title: '빌드 상태 조회',
    description: '등록된 빌드 ID로 CI와 이미지 게시 상태를 조회합니다. 게시만으로 앱 배포가 완료되지는 않습니다.',
    schema: z.object({ build_id: resourceId }).strict(), readOnly: true,
    run: (api, { build_id }) => api.build(build_id),
  },
  deploy_repository: {
    title: '공개 GitHub 저장소 배포 요청',
    description: '명시된 앱·대상에 공개 GitHub 저장소를 배포합니다. 호출 전 사용자의 별도 승인이 필요합니다. 접수 결과는 배포 완료가 아닙니다.',
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
