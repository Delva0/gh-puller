// 引擎级回归:定义注册 → 事件翻译 → assembler 折叠 → 断言 chat 视图有节点。
import { describe, it, expect } from 'vitest';
import {
  ConversationEventRegistry,
  ConversationViewRegistry,
  ConversationNodeAssembler,
  type ConversationEventInput,
} from '../../runtime/src/client';
import { registerConversationNodes } from '../../ui-conversation/src/client/conversation-nodes/register';
import { apply as applyTrajectory } from '../../ui-trajectory/src/client';
import { deriveTrajectoryLayout } from '../../ui-trajectory/src/client/layout';
import { GhToDshEvents } from '../dsh-events';
import type { EventEnvelope } from '../../../../monitor-data/types';

function evt(type: string, seq: number, data: Record<string, unknown>): EventEnvelope {
  return { id: `e${seq}`, seq, ts: 1700000000.5, session: 'ns/u1', type, data };
}

function face(events: ConversationEventRegistry, views: ConversationViewRegistry) {
  return {
    conversationEvents: events,
    conversationViews: views,
    slots: { inject: () => () => {}, register: () => () => {} } as never,
    locale: { register: () => {}, bind: () => () => '' },
    hostDescription: { getSnapshot: () => undefined, subscribe: () => () => {} },
    loadOlder: async () => false,
  };
}

function assemble(events: ConversationEventRegistry, views: ConversationViewRegistry, raws: EventEnvelope[]) {
  const adapter = new GhToDshEvents();
  const entries: ConversationEventInput[] = [];
  for (const raw of raws) {
    for (const event of adapter.translate(raw)) entries.push({ event, view: undefined });
  }
  const assembler = new ConversationNodeAssembler(events, views);
  assembler.replaceWindow(entries, false);
  assembler.flush();
  return assembler;
}

describe('engine assemble', () => {
  it('定义注册 + 翻译 + 折叠产出 chat 节点', () => {
    const events = new ConversationEventRegistry();
    const views = new ConversationViewRegistry();
    registerConversationNodes(face(events, views));
    const chat = assemble(events, views, [
      evt('turn/start', 1, { turn: 1 }),
      evt('step/start', 2, { turn: 1, step: 1 }),
      evt('user/message', 3, {
        turn: 1, step: 1,
        message: { role: 'user', content: [{ type: 'text', text: 'probe' }] },
        source: { kind: 'user' }, surfaceOp: 'append',
      }),
      evt('assistant/message', 4, {
        turn: 1, step: 1,
        message: { role: 'assistant', content: [{ type: 'content', text: 'hello' }] },
        surfaceOp: 'append',
      }),
      evt('step/end', 5, { turn: 1, step: 1 }),
      evt('turn/end', 6, { turn: 1 }),
    ]).get('chat');

    expect((chat?.order?.length ?? 0)).toBeGreaterThan(0);
  });

  it('chunk 展开 + 按步合并:折叠不抛、行序 assistant 在 tool 前、合并块未丢失', () => {
    const events = new ConversationEventRegistry();
    const views = new ConversationViewRegistry();
    registerConversationNodes(face(events, views));
    const assembler = assemble(events, views, [
      evt('turn/start', 1, { turn: 1 }),
      evt('step/start', 2, { turn: 1, step: 1 }),
      evt('user/message', 3, {
        turn: 1, step: 1,
        message: { role: 'user', content: [{ type: 'text', text: '流式块' }] },
        source: { kind: 'user' }, surfaceOp: 'append',
      }),
      // 流式增量(live 通道):content/thinking/tool_call 三块
      evt('assistant/chunk', 4, { turn: 1, step: 1, chunk: { type: 'content', index: 0, text: 'a' } }),
      evt('assistant/chunk', 5, { turn: 1, step: 1, chunk: { type: 'thinking', index: 1, text: '想' } }),
      evt('assistant/chunk', 6, { turn: 1, step: 1, chunk: { type: 'tool_call', index: 2, partial_json: '{"cmd":"ls"}' } }),
      // 段消息:thinking → 空(usage) → (工具执行后)tool_call —— 合并为一条全量
      evt('assistant/message', 7, {
        turn: 1, step: 1,
        message: { role: 'assistant', content: [{ type: 'thinking', text: '想法' }] },
        usage: { input: 1, output: 1 }, surfaceOp: 'append',
      }),
      evt('assistant/message', 8, {
        turn: 1, step: 1, message: { role: 'assistant', content: [] },
        usage: { input: 2, output: 1 }, surfaceOp: 'append',
      }),
      evt('tool/call', 9, { turn: 1, step: 1, callId: 'c1', name: 'Bash', arguments: '{"cmd":"ls"}' }),
      evt('tool/result', 10, {
        turn: 1, step: 1, callId: 'c1', is_error: false,
        message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'ok' }] },
        surfaceOp: 'append',
      }),
      evt('assistant/message', 11, {
        turn: 1, step: 1,
        message: { role: 'assistant', content: [{ type: 'tool_call', id: 'c1', name: 'Bash', input: { cmd: 'ls' } }] },
        surfaceOp: 'append',
      }),
      evt('step/end', 12, { turn: 1, step: 1 }),
      evt('step/start', 13, { turn: 1, step: 2 }),
      evt('assistant/message', 14, {
        turn: 1, step: 2,
        message: { role: 'assistant', content: [{ type: 'content', text: '完成' }] },
        surfaceOp: 'append',
      }),
      evt('step/end', 15, { turn: 1, step: 2 }),
      evt('turn/end', 16, { turn: 1 }),
    ]);
    const chat = assembler.get('chat');

    expect((chat?.order?.length ?? 0)).toBeGreaterThan(0);
    const kinds = (chat?.order ?? []).map((key) => chat!.nodes.get(key)?.kind);
    // 行序:user → assistant-step(合并,锚=7) → tool-call(9) → assistant-step(次步,14) → turn-tail
    expect(kinds).toEqual(['user', 'assistant-step', 'tool-call', 'assistant-step', 'turn-tail']);
    // 合并块未丢失:assistant-step 节点含 reasoning+text?+tool-call 块
    const firstAssistant = chat!.nodes.get(chat!.order[kinds.indexOf('assistant-step')]);
    const data = firstAssistant?.data as { blocks: Array<{ type: string }> };
    expect(data.blocks.length).toBeGreaterThan(0);
  });

  it('纯工具步骤仍按原始事件顺序把用户输入放在首个工具前', () => {
    const events = new ConversationEventRegistry();
    const views = new ConversationViewRegistry();
    const registration = face(events, views);
    registerConversationNodes(registration);
    applyTrajectory(registration);
    const trajectory = assemble(events, views, [
      evt('turn/start', 2, { turn: 1 }),
      evt('step/start', 3, { turn: 1, step: 1 }),
      evt('user/message', 4, {
        turn: 1, step: 1,
        message: { role: 'user', content: [{ type: 'text', text: 'prompt' }] },
        source: { kind: 'user' }, surfaceOp: 'append',
      }),
      evt('tool/call', 5, { turn: 1, step: 1, callId: 'c1', name: 'bash', arguments: '{}' }),
      evt('tool/result', 6, {
        turn: 1, step: 1, callId: 'c1', is_error: false,
        message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'ok' }] },
        surfaceOp: 'append',
      }),
      evt('step/end', 7, { turn: 1, step: 1 }),
      evt('step/start', 8, { turn: 1, step: 2 }),
      evt('tool/call', 9, { turn: 1, step: 2, callId: 'c2', name: 'bash', arguments: '{}' }),
      evt('tool/result', 10, {
        turn: 1, step: 2, callId: 'c2', is_error: false,
        message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'c2', content: 'ok' }] },
        surfaceOp: 'append',
      }),
      evt('step/end', 11, { turn: 1, step: 2 }),
      evt('turn/end', 12, { turn: 1, reason: 'aborted' }),
    ]).get('trajectory');

    const turns = deriveTrajectoryLayout({
      nodes: trajectory?.eventNodes ?? [],
      eventLocations: trajectory?.eventLocations,
      partial: trajectory?.partial ?? null,
      runningCalls: trajectory?.runningCalls ?? [],
      requests: trajectory?.requests,
      callSchemas: trajectory?.callSchemas,
    });
    expect(turns.flatMap(turn => turn.groups.flatMap(group => group.cells.map(cell => cell.kind))))
      .toEqual(['user', 'tool', 'tool']);
  });
});
