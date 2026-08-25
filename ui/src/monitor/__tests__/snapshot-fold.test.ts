import { describe, expect, it } from 'vitest';
import { RunFold } from '../fold';
import { buildSnapshot } from '../snapshot';
import { deriveTrajectoryTimeline, trajectoryTimelineFocusIndexes } from '../timeline';
import { deriveTrajectoryLayout } from '../layout';
import { TrajectorySearchIndex } from '../search-index';
import { contextProvenance } from '../provenance';
import { mergeEvents } from '../ws';
import type { EventEnvelope, RequestView } from '../types';

let n = 0;
function evt(type: string, seq: number, data: Record<string, unknown> = {}) {
  return { id: `e-${n++}`, seq, ts: 1000 + seq, session: 's1', type, data } as EventEnvelope;
}

function ccRun(): EventEnvelope[] {
  return [
    evt('session/start', 0, { label: 'wiki:structure', provider: 'claude', model: '' }),
    evt('turn/start', 1, { turn: 1 }),
    evt('step/start', 2, { turn: 1, step: 1 }),
    evt('user/message', 3, { message: { role: 'user', content: [{ type: 'text', text: 'q' }] }, source: { kind: 'user' }, surfaceOp: 'append' }),
    evt('request/header', 4, { header: { config: { provider: 'claude', model: '' } }, reason: 'initial', partial: true }),
    evt('assistant/chunk', 5, { chunk: { type: 'text', index: 0, text: '抽' } }),
    evt('assistant/chunk', 6, { chunk: { type: 'text', index: 0, text: '丝' } }),
    evt('assistant/message', 7, { message: { role: 'assistant', content: [{ type: 'text', text: '抽丝' }] }, surfaceOp: 'append', sourceSeqs: [5, 6], usage: { input_tokens: 4, output_tokens: 2 } }),
    evt('step/end', 8, { turn: 1, step: 1 }),
    evt('session/end', 9, { state: 'completed', ok: true, duration_ms: 40, text_chars: 2, num_steps: 1 }),
  ];
}

describe('RunFold(live/历史接缝)', () => {
  it('ingest 顺序守卫:重复忽略、间隙报告、补片后重放一致', () => {
    const f = new RunFold();
    for (const e of ccRun().slice(0, 5)) expect(f.ingest(e)).toBe('ok');
    expect(f.ingest(evt('assistant/chunk', 3, { chunk: { type: 'text', index: 0, text: 'dup' } }))).toBe('dup');
    // 间隙:next=5(已收 0..4),live 来 seq=7
    expect(f.ingest(evt('assistant/chunk', 7, { chunk: { type: 'text', index: 0, text: 'x' } }))).toBe('gap');
    expect(f.requestedFrom()).toBe(5);
    // 补片 5..9 后重放:消息上下文完整(partial 消失,因 assistant/message 已定型)
    f.applyBatch(ccRun().slice(5));
    expect(f.length).toBe(10);
    expect(f.messages().map((m) => (m.content[0] as { text?: string }).text)).toEqual(['q', '抽丝']);
    expect(f.partial).toBe('');
    const header = f.headerAt()?.data.header as { config?: unknown };
    expect(header?.config).toEqual({ provider: 'claude', model: '' });
  });

  it('流式 partial:未定型时拼接增量', () => {
    const f = new RunFold();
    for (const e of [evt('user/message', 0, { message: { role: 'user', content: [{ type: 'text', text: 'q' }] }, surfaceOp: 'append' }), evt('assistant/chunk', 1, { chunk: { type: 'text', index: 0, text: 'hi' } }), evt('assistant/chunk', 2, { chunk: { type: 'text', index: 0, text: '世界' } })]) {
      expect(f.ingest(e)).toBe('ok');
    }
    expect(f.partial).toBe('hi世界');
  });

  it('applyBatch 容忍 seq 洞(文件侧投影契约定域:chunk 被跳过后不触发 gap)', () => {
    // 磁盘投影 seq 0,1,2,5,7(chunk 3/4/6 未落盘)→ 洞是合法结构
    const f = new RunFold();
    const fileside = [
      evt('session/start', 0),
      evt('user/message', 1, { message: { role: 'user', content: [{ type: 'text', text: 'q' }] }, surfaceOp: 'append' }),
      evt('step/start', 2),
      evt('assistant/message', 5, { message: { role: 'assistant', content: [{ type: 'text', text: 'aidan' }] }, surfaceOp: 'append' }),
      evt('session/end', 7, { state: 'completed', ok: true, duration_ms: 1, text_chars: 5, num_steps: 1 }),
    ];
    f.applyBatch(fileside);
    expect(f.length).toBe(8); // nextSeq = 窗口尾 seq+1,洞不使守卫开裂
    expect(f.messages().map((m) => (m.content[0] as { text?: string }).text)).toEqual(['q', 'aidan']);
    // live 接续:seq 8 直接按 ok 折入(不再补片);早于 nextSeq → dup
    expect(f.ingest(evt('assistant/chunk', 8, { chunk: { type: 'text', index: 0, text: 'x' } }))).toBe('ok');
    expect(f.ingest(evt('turn/start', 3))).toBe('dup');
  });
});

describe('buildSnapshot(对话节点+请求序)', () => {
  it('请求/工具/用法归并;partial 与在途调用可见', () => {
    const snap = buildSnapshot(ccRun());
    expect(snap.requests).toHaveLength(1);
    const req = snap.requests[0];
    expect(req.text).toBe('抽丝');
    expect(req.usage).toEqual({ input_tokens: 4, output_tokens: 2 });
    expect(snap.chatNodes.map((c) => c.kind)).toContain('user');
    expect(snap.chatNodes.map((c) => c.kind)).toContain('assistant');
    expect(snap.partial).toBe('');
  });

  it('工具调用/结果配对与在途集', () => {
    const events: EventEnvelope[] = [
      evt('user/message', 0, { message: { role: 'user', content: [{ type: 'text', text: 'q' }] }, surfaceOp: 'append' }),
      evt('assistant/message', 1, { message: { role: 'assistant', content: [{ type: 'text', text: 'a' }] }, surfaceOp: 'append' }),
      evt('tool/call', 2, { callId: 't1', name: 'graphify_query', arguments: '{"q":"x"}' }),
      evt('assistant/chunk', 3, { chunk: { type: 'text', index: 0, text: '流' } }),
      evt('assistant/chunk', 4, { chunk: { type: 'text', index: 0, text: '式' } }),
    ];
    const snap = buildSnapshot(events);
    expect(snap.runningCalls.map((t) => t.callId)).toEqual(['t1']);
    expect(snap.partial).toBe('流式');
    const withResult = buildSnapshot([...events, evt('tool/result', 5, { callId: 't1', is_error: false, message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 't1', content: '结果', is_error: false }] }, surfaceOp: 'append' })]);
    expect(withResult.runningCalls).toEqual([]);
    expect(withResult.requests[0].tools[0].result).toBe('结果');
  });

  it('request/header 产生系统卡;context/inject 产生上下文行', () => {
    const events: EventEnvelope[] = [
      evt('request/header', 0, { header: { config: { model: 'm1' }, system: 's' }, reason: 'initial' }),
      evt('context/inject', 1, { target: 'user-message', provenance: 'deepwiki:note', text: '<note>指引</note>' }),
    ];
    const snap = buildSnapshot(events);
    expect(snap.chatNodes.find((c) => c.kind === 'system')?.header?.config?.model).toBe('m1');
    expect(snap.chatNodes.find((c) => c.kind === 'context')?.contextText).toBe('<note>指引</note>');
  });
});

describe('时间轴/布局/搜索', () => {
  const reqs: RequestView[] = [
    { seq: 3, step: 1, turn: 1, ts: 2000, durationMs: 500, text: 'a', thinking: '', tools: [] },
    { seq: 10, step: 2, turn: 1, ts: 3000, durationMs: 1500, text: 'b', thinking: '', tools: [] },
    { seq: 20, step: 3, turn: 1, ts: 4000, durationMs: 250, text: 'c', thinking: '', tools: [] },
  ];

  it('四种时间轴模式比例与范围选择', () => {
    const seq = deriveTrajectoryTimeline(reqs, 'sequence');
    expect(seq.points.map((p) => Math.round(p.offset * 100))).toEqual([0, 50, 100]);
    const dur = deriveTrajectoryTimeline(reqs, 'duration');
    expect(dur.ticks[0]).toBe('500ms'); // 标签取该请求自身时长
    expect(dur.points[1].offset).toBeCloseTo(2000 / 2250, 3); // 累计时长
    expect(deriveTrajectoryTimeline(reqs, 'actual').points.map((p) => Math.round(p.offset * 100))).toEqual([0, 41, 100]);
    const [a, b] = trajectoryTimelineFocusIndexes(dur.points, { start: 10, end: 20 });
    expect([a, b]).toEqual([1, 2]);
  });

  it('布局按 step 归组(请求+工具单元格)', () => {
    const snap = buildSnapshot(ccRun());
    const groups = deriveTrajectoryLayout(snap.requests, snap.chatNodes);
    expect(groups.map((g) => g.step)).toEqual([1]);
    expect(groups[0].cells.filter((c) => c.kind === 'request')).toHaveLength(1);
  });

  it('搜索命中节点 seq;空查询不变', () => {
    const idx = new TrajectorySearchIndex();
    idx.setNodes(buildSnapshot(ccRun()).chatNodes);
    expect(idx.query('抽丝').size).toBeGreaterThan(0);
    expect(idx.query('   ').size).toBe(0);
    expect(idx.query('不存在的词').size).toBe(0);
  });

  it('source 投影:用户 vs 注入', () => {
    expect(contextProvenance({ kind: 'user' }).kind).toBe('user');
    expect(contextProvenance({ kind: 'context', label: 'deepwiki:note' }).label).toBe('deepwiki:note');
    expect(contextProvenance({ kind: 'context', form: 'notice' }).form).toBe('notice');
  });

  it('mergeEvents 按 seq 去重合并', () => {
    const merged = mergeEvents(ccRun().slice(0, 5), ccRun().slice(4));
    expect(merged.map((e) => e.seq)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
  });
});
