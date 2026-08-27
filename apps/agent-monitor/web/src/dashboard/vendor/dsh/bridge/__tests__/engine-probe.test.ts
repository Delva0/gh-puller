// 引擎级回归:定义注册 → 事件翻译 → assembler 折叠 → 断言 chat 视图有节点。
import { describe, it, expect } from 'vitest';
import {
  ConversationEventRegistry,
  ConversationViewRegistry,
  ConversationNodeAssembler,
  type ConversationEventInput,
} from '../../runtime/src/client';
import { registerConversationNodes } from '../../ui-conversation/src/client/conversation-nodes/register';
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
    loadOlder: async () => false,
  };
}

describe('engine assemble', () => {
  it('定义注册 + 翻译 + 折叠产出 chat 节点', () => {
    const events = new ConversationEventRegistry();
    const views = new ConversationViewRegistry();
    registerConversationNodes(face(events, views));
    const adapter = new GhToDshEvents();
    const entries: ConversationEventInput[] = [];
    for (const raw of [
      evt('turn/start', 1, { turn: 1 }),
      evt('step/start', 2, { turn: 1, step: 1 }),
      evt('user/message', 3, {
        turn: 1, step: 1,
        message: { role: 'user', content: [{ type: 'text', text: 'probe' }] },
        source: { kind: 'user' }, surfaceOp: 'append',
      }),
      evt('assistant/message', 4, {
        turn: 1, step: 1,
        message: { role: 'assistant', content: [{ type: 'text', text: 'hello' }] },
        surfaceOp: 'append',
      }),
      evt('turn/end', 5, { turn: 1 }),
    ]) {
      for (const event of adapter.translate(raw)) entries.push({ event, view: undefined });
    }
    const assembler = new ConversationNodeAssembler(events, views);
    assembler.replaceWindow(entries, false);
    assembler.flush();
    const chat = assembler.get('chat');

    expect((chat?.order?.length ?? 0)).toBeGreaterThan(0);
  });
});
