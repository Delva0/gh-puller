// gh→dsh 事件适配器单测:表面事件直传、chunk 块语言合成、按步合并、词汇外事件丢弃。
// 禁真机:纯函数,无网络无 LLM。
import { describe, expect, it } from 'vitest';
import { BLOCK_START_SEQ_OFFSET, GhToDshEvents } from '../dsh-events';
import type { EventEnvelope } from '../../../../monitor-data/types';

function evt(type: string, seq: number, data: Record<string, unknown>): EventEnvelope {
  return { id: `e${seq}`, seq, ts: 1700000000.5, session: 'ns/u1', type, data };
}

/** 依次翻译并聚总输出(dsh 事件数组,按输出顺序)。 */
function run(adapter: GhToDshEvents, events: Array<[string, number, Record<string, unknown>]>) {
  const out: unknown[] = [];
  for (const [type, seq, data] of events) out.push(...adapter.translate(evt(type, seq, data)));
  return out as Array<{ type: string; seq: number; surfaceOp?: unknown; data: { [k: string]: unknown } }>;
}

describe('GhToDshEvents', () => {
  it('user/message 直传:type/seq/time(秒→毫秒)/surfaceOp 提升到顶层', () => {
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
      chunk: { type: 'content', index: 0, text: '世' },
    }));
    expect(first).toHaveLength(2);
    expect(first[0]).toMatchObject({ type: 'assistant/chunk', data: { chunk: { type: 'block-start', index: 0, blockType: 'text' } } });
    expect(first[1]).toMatchObject({ data: { chunk: { type: 'text-delta', index: 0, text: '世' } } });
    // block-start 与 delta 必须独占不同的 seq(dsh assembler 逐 context 断言序严格递增)
    expect(first[0].seq).toBe(5 + BLOCK_START_SEQ_OFFSET);
    expect(first[1].seq).toBe(5);

    const second = adapter.translate(evt('assistant/chunk', 6, {
      turn: 1, step: 2,
      chunk: { type: 'content', index: 0, text: '界' },
    }));
    expect(second).toHaveLength(1); // 只有 delta
    expect(second[0].data).toMatchObject({ chunk: { type: 'text-delta', index: 0, text: '界' } });
  });

  it('chunk 词表:thinking→reasoning / tool_call(及 legacy tool_input)→tool-call', () => {
    const adapter = new GhToDshEvents();
    const [start] = adapter.translate(evt('assistant/chunk', 7, {
      turn: 1, step: 1,
      chunk: { type: 'thinking', index: 0, text: '嗯…' },
    }));
    expect(start.data).toMatchObject({ chunk: { type: 'block-start', blockType: 'reasoning' } });

    for (const [chunkType, index] of [['tool_call', 1], ['tool_input', 2]] as const) {
      const toolBlocks = adapter.translate(evt('assistant/chunk', 8, {
        turn: 1, step: 1,
        chunk: { type: chunkType, index, partial_json: '{"a":1}' },
      }));
      expect(toolBlocks[0].data).toMatchObject({ chunk: { type: 'block-start', blockType: 'tool-call' } });
      expect(toolBlocks[1].data).toMatchObject({ chunk: { type: 'tool-call-delta', argumentsDelta: '{"a":1}' } });
    }
  });

  it('按步合并:段消息累加为一条全量消息,工具事件步末按原 seq 吐,seq 全局递增', () => {
    const adapter = new GhToDshEvents();
    const out = run(adapter, [
      ['step/start', 3, { turn: 1, step: 1 }],
      ['user/message', 4, {
        turn: 1, step: 1,
        message: { role: 'user', content: [{ type: 'text', text: 'hi' }] },
        source: { kind: 'user' }, surfaceOp: 'append',
      }],
      // 段1:思考(协议词表 thinking{text});下面三条消息翻译均返回 [] …
      ['assistant/message', 5, {
        turn: 1, step: 1,
        message: { role: 'assistant', content: [{ type: 'thinking', text: '思路' }] },
        usage: { input: 5, output: 1 },
        surfaceOp: 'append',
      }],
      // 段2:空 content(usage 终结标记;协议派生 None)
      ['assistant/message', 6, {
        turn: 1, step: 1,
        message: { role: 'assistant', content: [] },
        usage: { input: 10, output: 2 },
        surfaceOp: 'append',
      }],
      ['tool/call', 7, { turn: 1, step: 1, callId: 'c1', name: 'Bash', arguments: '{}' }],
      ['tool/result', 8, {
        turn: 1, step: 1, callId: 'c1', is_error: false,
        message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'c1', content: 'ok' }] },
        surfaceOp: 'append',
      }],
      // 段3:工具调用块(tool_call{id,name,input});在 tool/result 之后到达(gh 实测顺序)
      ['assistant/message', 9, {
        turn: 1, step: 1,
        message: {
          role: 'assistant',
          content: [{ type: 'tool_call', id: 'c1', name: 'Bash', input: { cmd: 'ls' } }],
        },
        surfaceOp: 'append',
      }],
      ['step/end', 10, { turn: 1, step: 1 }],
    ]);
    // 步末一次吐出:合并消息(锚=首条非空段消息 seq=5)→ 工具事件(原 seq)→ step/end
    // (step/start、user/message 为边界前事件,立即直发)
    expect(out).toHaveLength(6);
    const msg = out[2], call = out[3], result = out[4], end = out[5];
    expect(msg.type).toBe('assistant/message');
    expect(msg.seq).toBe(5);
    expect(msg).toMatchObject({ surfaceOp: 'append' });
    const content = (msg.data.message as { content: Array<{ type: string; text?: unknown }> }).content;
    expect(content).toEqual([
      { type: 'reasoning', text: '思路' },
      { type: 'tool-call', id: 'c1', name: 'Bash', arguments: '{"cmd":"ls"}' },
    ]);
    expect(msg.data.usage).toMatchObject({ inputTokens: 10, outputTokens: 2 }); // 最后一条带 usage 的段
    expect(call.type).toBe('tool/call');
    expect(call.data).toMatchObject({ callId: 'c1', name: 'Bash', arguments: '{}' });
    expect(result.type).toBe('tool/result');
    // 单层包裹:消息 content[0] 即 tool-result 块,content[0].content 是文本块而非嵌套 tool-result
    const rmsg = result.data.message as { content: Array<{ type: string; content: unknown[] }> };
    expect(rmsg.content[0]).toMatchObject({ type: 'tool-result', toolCallId: 'c1', isError: false });
    expect(rmsg.content[0].content[0]).toMatchObject({ type: 'text', text: 'ok' });
    expect(end.type).toBe('step/end');
    expect(msg.seq).toBeLessThan(call.seq);
    expect(call.seq).toBeLessThan(result.seq);
    expect(result.seq).toBeLessThan(end.seq);
  });

  it('纯空段歩:不发射 assistant/message,仅吐工具事件', () => {
    const adapter = new GhToDshEvents();
    const out = run(adapter, [
      ['step/start', 1, { turn: 1, step: 1 }],
      ['assistant/message', 2, {
        turn: 1, step: 1, message: { role: 'assistant', content: [] },
        usage: { input: 3, output: 0 }, surfaceOp: 'append',
      }],
      ['tool/call', 3, { turn: 1, step: 1, callId: 'c1', name: 'Bash', arguments: '{}' }],
      ['step/end', 4, { turn: 1, step: 1 }],
    ]);
    expect(out.map((e) => e.type)).toEqual(['step/start', 'tool/call', 'step/end']);
  });

  it('reset() 一并清空合并缓冲与 chunk 状态机', () => {
    const adapter = new GhToDshEvents();
    adapter.translate(evt('assistant/message', 1, {
      turn: 1, step: 1,
      message: { role: 'assistant', content: [{ type: 'thinking', text: '半' }] },
      surfaceOp: 'append',
    }));
    adapter.translate(evt('tool/call', 2, { turn: 1, step: 1, callId: 'c1', name: 'x', arguments: '{}' }));
    adapter.reset();
    // 重置后启动新步:旧缓冲不得再泄出,新步末只吐本步内容
    const out = run(adapter, [
      ['step/start', 5, { turn: 1, step: 2 }],
      ['tool/call', 6, { turn: 1, step: 2, callId: 'c2', name: 'x', arguments: '{}' }],
      ['step/end', 7, { turn: 1, step: 2 }],
    ]);
    expect(out.map((e) => e.type)).toEqual(['step/start', 'tool/call', 'step/end']);
    // chunk 状态机同样被清空(换会话后同 key 再发 block-start)
    adapter.reset();
    const chunkOut = adapter.translate(evt('assistant/chunk', 21, { turn: 1, step: 1, chunk: { type: 'content', index: 0, text: 'b' } }));
    expect(chunkOut[0].data).toMatchObject({ chunk: { type: 'block-start', index: 0 } });
  });

  it('dsh 词汇外事件丢弃(session/start、session/end、error、context/*)', () => {
    const adapter = new GhToDshEvents();
    expect(adapter.translate(evt('session/start', 0, { run_id: 'r' }))).toHaveLength(0);
    expect(adapter.translate(evt('session/end', 12, { state: 'completed' }))).toHaveLength(0);
    expect(adapter.translate(evt('error', 13, { message: 'x' }))).toHaveLength(0);
    expect(adapter.translate(evt('context/inject', 14, { target: 'p', text: 'z' }))).toHaveLength(0);
    expect(adapter.translate(evt('context/modify', 15, { target: 'p', kind: 'trim' }))).toHaveLength(0);
  });
});
