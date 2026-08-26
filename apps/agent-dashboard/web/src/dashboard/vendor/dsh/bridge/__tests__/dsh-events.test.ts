// gh→dsh 事件适配器单测:表面事件直传、chunk 块语言合成、词汇外事件丢弃。
// 禁真机:纯函数,无网络无 LLM。
import { describe, expect, it } from 'vitest';
import { GhToDshEvents } from '../dsh-events';
import type { EventEnvelope } from '../../../../monitor-data/types';

function evt(type: string, seq: number, data: Record<string, unknown>): EventEnvelope {
  return { id: `e${seq}`, seq, ts: 1700000000.5, session: 'ns/u1', type, data };
}

describe('GhToDshEvents', () => {
  it('表面事件直传:type/seq/time(秒→毫秒)/surfaceOp', () => {
    const adapter = new GhToDshEvents();
    const [out] = adapter.translate(evt('user/message', 3, {
      message: { role: 'user', content: [{ type: 'text', text: 'hi' }] },
      surfaceOp: 'append',
    }));
    expect(out.type).toBe('user/message');
    expect(out.seq).toBe(3);
    expect(out.time).toBe(1700000000500);
    expect(out).toMatchObject({ surfaceOp: 'append' }); // surfaceOp 在事件顶层(dsh 契约)
  });

  it('assistant/chunk:同 (turn,step,index) 首块发 block-start,其后仅复播 delta', () => {
    const adapter = new GhToDshEvents();
    const first = adapter.translate(evt('assistant/chunk', 5, {
      turn: 1, step: 2,
      chunk: { type: 'text', index: 0, text: '世' },
    }));
    expect(first).toHaveLength(2);
    expect(first[0]).toMatchObject({ type: 'assistant/chunk', data: { chunk: { type: 'block-start', index: 0, blockType: 'text' } } });
    expect(first[1]).toMatchObject({ data: { chunk: { type: 'text-delta', index: 0, text: '世' } } });

    const second = adapter.translate(evt('assistant/chunk', 6, {
      turn: 1, step: 2,
      chunk: { type: 'text', index: 0, text: '界' },
    }));
    expect(second).toHaveLength(1); // 只有 delta
    expect(second[0].data).toMatchObject({ chunk: { type: 'text-delta', index: 0, text: '界' } });
  });

  it('thinking/tool_input 增量映射 reasoning / tool-call 块', () => {
    const adapter = new GhToDshEvents();
    const [start] = adapter.translate(evt('assistant/chunk', 7, {
      turn: 1, step: 1,
      chunk: { type: 'thinking', index: 0, text: '嗯…' },
    }));
    expect(start.data).toMatchObject({ chunk: { type: 'block-start', blockType: 'reasoning' } });

    const toolBlocks = adapter.translate(evt('assistant/chunk', 8, {
      turn: 1, step: 1,
      chunk: { type: 'tool_input', index: 1, partial_json: '{"a":1}' },
    }));
    expect(toolBlocks[0].data).toMatchObject({ chunk: { type: 'block-start', blockType: 'tool-call' } });
    expect(toolBlocks[1].data).toMatchObject({ chunk: { type: 'tool-call-delta', argumentsDelta: '{"a":1}' } });
  });

  it('assistant/message 直传并映射 usage 字段(dsh 名)', () => {
    const adapter = new GhToDshEvents();
    const [out] = adapter.translate(evt('assistant/message', 9, {
      turn: 1, step: 1,
      message: { role: 'assistant', content: [{ type: 'text', text: '答' }] },
      usage: { input: 10, output: 20 },
      surfaceOp: 'append',
    }));
    expect(out.data).toMatchObject({ usage: { inputTokens: 10, outputTokens: 20 } });
  });

  it('tool/call 与 tool/result 映射(轮次/步骤/关联 id)', () => {
    const adapter = new GhToDshEvents();
    const [call] = adapter.translate(evt('tool/call', 10, {
      turn: 1, step: 1, callId: 'c1', name: 'fetch', arguments: '{}',
    }));
    expect(call.data).toMatchObject({ callId: 'c1', name: 'fetch', arguments: '{}' });
    const [result] = adapter.translate(evt('tool/result', 11, {
      turn: 1, step: 1, callId: 'c1', is_error: true,
      message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'boom' }] },
      surfaceOp: 'append',
    }));
    const msg = result.data as { message: { content: Array<{ toolCallId: string; isError?: boolean }>; source: { callId: string } } };
    expect(msg.message.content[0]).toMatchObject({ toolCallId: 'c1', isError: true });
    expect(msg.message.source).toMatchObject({ callId: 'c1' });
  });

  it('dsh 词汇外事件丢弃(session/start、session/end、error、context/*)', () => {
    const adapter = new GhToDshEvents();
    expect(adapter.translate(evt('session/start', 0, { run_id: 'r' }))).toHaveLength(0);
    expect(adapter.translate(evt('session/end', 12, { state: 'completed' }))).toHaveLength(0);
    expect(adapter.translate(evt('error', 13, { message: 'x' }))).toHaveLength(0);
    expect(adapter.translate(evt('context/inject', 14, { target: 'p', text: 'z' }))).toHaveLength(0);
    expect(adapter.translate(evt('context/modify', 15, { target: 'p', kind: 'trim' }))).toHaveLength(0);
  });

  it('reset() 清空 chunk 状态机(换会话后同 key 再发 block-start)', () => {
    const adapter = new GhToDshEvents();
    adapter.translate(evt('assistant/chunk', 20, { turn: 1, step: 1, chunk: { type: 'text', index: 0, text: 'a' } }));
    adapter.reset();
    const out = adapter.translate(evt('assistant/chunk', 21, { turn: 1, step: 1, chunk: { type: 'text', index: 0, text: 'b' } }));
    expect(out[0].data).toMatchObject({ chunk: { type: 'block-start', index: 0 } });
  });
});
