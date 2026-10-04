import { McpServer } from '@modelcontextprotocol/server';
import { createToolRunner, tools } from './tools.js';

export function createToolServer(api) {
  const server = new McpServer({ name: 'railshot-agent-tools', version: '0.1.0' }, {
    instructions: '배포 소스는 공개 GitHub 저장소 URL만 지원합니다. 로컬 폴더·파일·ZIP 업로드 배포는 지원하지 않습니다. 배포 접수 후 get_deployment_progress를 호출하고 poll_after_ms 뒤 activity_cursor를 since로 전달해 반복 조회하세요. deployment_updated이면 배포 단계·상태를, agent_activity_updated이면 AI 작업 요약·현재 작업·변경 파일·검증 결과를 사용자에게 알리세요. run_attempt_changed이면 이전 시도의 결과로 설명하지 마세요. events_state가 unavailable 또는 stale_response이면 최신 AI 작업을 확인했다고 주장하지 마세요. 배포 실패 시 검증된 원인과 CI 로그를 설명하되 agent_activity.state=succeeded는 배포 성공이 아닙니다. MCP 서버는 대화창에 스스로 메시지를 보내지 못하므로 자동 모니터링은 MCP 클라이언트가 반복 호출해야 합니다.',
  });
  const call = createToolRunner(api);
  for (const [name, tool] of Object.entries(tools)) {
    server.registerTool(name, {
      title: tool.title, description: tool.description, inputSchema: tool.schema,
      annotations: { readOnlyHint: tool.readOnly, destructiveHint: false, idempotentHint: tool.readOnly, openWorldHint: false },
    }, async (args) => {
      try {
        const value = await call(name, args);
        return { content: [{ type: 'text', text: JSON.stringify(value) }], structuredContent: value };
      } catch (error) {
        const value = { code: error.code || 'TOOL_ERROR',
          message: error.name === 'ZodError' ? '도구 입력이 잘못되었습니다.' : error.message,
          outcome_unknown: error.outcomeUnknown === true };
        return { content: [{ type: 'text', text: JSON.stringify(value) }], structuredContent: value, isError: true };
      }
    });
  }
  return server;
}
