import { describe, expect, it } from 'vitest';
import { applyEvent, deriveMessage, foldEvents, messagesAt, newSurface } from '../surface';
import type { EventEnvelope } from '../types';

let n = 0;
function evt(type: string, seq: number, data: Record<string, unknown> = {}) {
  return { id: `e-${n++}`, seq, ts: 1, session: 's1', type, data } as EventEnvelope;
}
function user(seq: number, text: string, surfaceOp: unknown = 'append') {
  return evt('user/message', seq, {
    message: { role: 'user', content: [{ type: 'text', text }] },
    source: { kind: 'user' },
    surfaceOp,
  });
}
function asst(seq: number, text: string) {
  return evt('assistant/message', seq, {
    message: { role: 'assistant', content: text ? [{ type: 'text', text }] : [] },
    surfaceOp: 'append',
  });
}
function toolResult(seq: number, callId: string, content: string) {
  return evt('tool/result', seq, {
    message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: callId, content, is_error: false }] },
    surfaceOp: 'append',
  });
}

const textOf = (m: { content: Array<Record<string, unknown>> }) =>
  m.content.map((b) => b.text ?? b.content ?? '').join('');

describe('surface 折叠(与 Python 契约同语义)', () => {
  it('append 顺序入面,derive 逐前缀恢复精确上下文', () => {
    const events = [evt('session/start', 0), user(1, 'q1'), asst(2, 'a1'), toolResult(3, 't1', 'r1'), asst(4, 'a2')];
    const s = foldEvents(events);
    expect(s.nodes).toEqual([1, 2, 3, 4]);
    // 请求 2 平面(seq=3):user + assistant
    expect(messagesAt(events, 3).map(textOf)).toEqual(['q1', 'a1']);
    // 全量
    expect(messagesAt(events, 99).map(textOf)).toEqual(['q1', 'a1', 'r1', 'a2']);
  });

  it('replace 遮蔽旧节点;replace 前旧节点可见', () => {
    const events = [user(0, 'old'), user(1, 'new', { op: 'replace', start: 0, end: 0 })];
    const s = foldEvents(events);
    expect(s.nodes).toEqual([1]);
    expect(messagesAt(events, 1).map((m) => (m.content[0] as { text?: string }).text)).toEqual(['old']);
    expect(messagesAt(events, 99).map((m) => (m.content[0] as { text?: string }).text)).toEqual(['new']);
  });

  it('空 content assistant 跳过;replace 引用不存在节点报错', () => {
    const empty = evt('assistant/message', 0, { message: { role: 'assistant', content: [] }, surfaceOp: 'append' });
    expect(deriveMessage(empty)).toBeNull();
    const s = newSurface();
    expect(() => applyEvent(s, user(0, 'x', { op: 'replace', start: 7, end: 7 }))).toThrow();
  });

  it('非 surface 事件不影响面', () => {
    const s = newSurface();
    applyEvent(s, evt('session/start', 0));
    applyEvent(s, evt('request/header', 1, { header: { config: {} }, reason: 'initial' }));
    expect(s.nodes).toEqual([]);
  });
});
