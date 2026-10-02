#!/usr/bin/env node
import { McpServer } from '@modelcontextprotocol/server';
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import { z } from 'zod';
import { isAbsolute } from 'node:path';
import { APP_NAME, TARGET_ID } from './contract.js';
import { deploySource, getRun, insideRoot } from './client.js';

const root = process.env.RAILSHOT_SOURCE_ROOT || process.env.JASMIN_SOURCE_ROOT;

const server = new McpServer({ name: 'railshot-api', version: '0.1.0' });

function result(value) {
  return { content: [{ type: 'text', text: JSON.stringify(value, null, 2) }], structuredContent: value };
}

function failure(error) {
  return { content: [{ type: 'text', text: error.message }], isError: true };
}

server.registerTool('deploy', {
  title: '앱 배포 또는 재배포',
  description: '사용자가 코드를 Railshot으로 배포해 달라고 하면 호출하세요. source는 공개 GitHub 저장소 URL 또는 MCP 호스트가 접근 가능한 로컬 ZIP/폴더의 절대 경로입니다. 첨부파일은 호스트가 RAILSHOT_SOURCE_ROOT 아래에 저장한 경로여야 하며, 이 서버가 대화의 첨부파일을 직접 읽지는 못합니다. 소스 형식은 자동 판별하고 기존 앱이면 변경된 파일만 반영한 뒤 Actions 검사와 이미지 게시를 다시 시작합니다. 이 도구의 성공은 대상 앱 배포 완료를 뜻하지 않습니다.',
  inputSchema: z.object({
    target_id: z.string().regex(TARGET_ID).optional().describe('API에 설정된 실행 대상 ID. 생략하면 API 설정을 사용'),
    source: z.string().min(1).describe('공개 GitHub 저장소 URL 또는 로컬 ZIP/폴더 절대 경로'),
    app: z.string().regex(APP_NAME).optional().describe('선택 사항. 생략하면 소스 이름으로 생성'),
  }),
  annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
}, async ({ app, source, target_id }) => {
  try {
    if (!/^[a-z][a-z\d+.-]*:\/\//i.test(source)) {
      if (!root) throw new Error('로컬 첨부파일을 배포하려면 RAILSHOT_SOURCE_ROOT를 설정하세요.');
      if (!isAbsolute(source)) throw new Error('로컬 소스는 절대 경로를 지정하세요.');
      source = await insideRoot(source, root);
    }
    return result(await deploySource({ source, targetId: target_id, ...(app ? { app } : {}) }));
  }
  catch (error) { return failure(error); }
});

server.registerTool('status', {
  title: '배포 상태',
  description: 'GitHub Actions 실행 ID로 CI 단계와 검증된 이미지 게시 산출물을 조회합니다. 앱 배포 성공이나 URL을 의미하지 않습니다.',
  inputSchema: z.object({ run_id: z.number().int().positive().describe('deploy가 반환한 run_id') }),
  annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
}, async ({ run_id }) => {
  try { return result(await getRun(run_id)); }
  catch (error) { return failure(error); }
});

await server.connect(new StdioServerTransport());
