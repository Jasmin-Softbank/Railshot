import { McpServer } from '@modelcontextprotocol/server';
import { createToolRunner, tools } from './tools.js';

export function createToolServer(api) {
  const server = new McpServer({ name: 'railshot-agent-tools', version: '0.1.0' });
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
