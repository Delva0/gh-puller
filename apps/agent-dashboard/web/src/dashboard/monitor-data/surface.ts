// 事件溯源 surface 折叠(与 dsh packages/core/session/src/surface.ts 同语义;
// gh-puller 单份规范实现,Python 侧契约测试见 tests/test_event_taxonomy.py)。

import type { EventEnvelope, Message, SurfaceOp } from './types';

export const SURFACE_TYPES = new Set(['user/message', 'assistant/message', 'tool/result']);

export interface Surface {
  nodes: number[]; // 当前可见 surface 节点 seq(模型可见顺序)
  bySeq: Map<number, EventEnvelope>;
}

export function newSurface(): Surface {
  return { nodes: [], bySeq: new Map() };
}

function surfaceOpOf(evt: EventEnvelope): SurfaceOp | null {
  const op = evt.data?.surfaceOp;
  if (op === 'append') return 'append';
  if (op && typeof op === 'object' && (op as { op?: string }).op === 'replace') {
    const o = op as { op: 'replace'; start: number; end: number };
    return { op: 'replace', start: Number(o.start), end: Number(o.end) };
  }
  return null;
}

/**
 * 单条事件应用进 surface;返回该面是否变化。
 * replace 引用不存在/倒置的节点 → 抛错(调用方先以 history 补片再重放,
 * 与 Python 侧 oracle 的"交换序升序重放"同一约束)。
 */
export function applyEvent(s: Surface, evt: EventEnvelope): boolean {
  if (!SURFACE_TYPES.has(evt.type)) return false;
  const op = surfaceOpOf(evt);
  if (!op) throw new Error(`surface 事件缺合法 surfaceOp: ${evt.type}`);
  s.bySeq.set(evt.seq, evt);
  if (op === 'append') {
    s.nodes.push(evt.seq);
    return true;
  }
  const si = s.nodes.indexOf(op.start);
  const ei = s.nodes.indexOf(op.end);
  if (si < 0 || ei < 0 || si > ei) {
    throw new Error(`replace 引用不存在/倒置的节点: ${JSON.stringify(op)}`);
  }
  s.nodes.splice(si, ei - si + 1, evt.seq);
  return true;
}

/** surface 事件 → 模型可见消息;空 content assistant → null(usage-only 不折入)。 */
export function deriveMessage(evt: EventEnvelope): Message | null {
  const msg = evt.data?.message as Message | undefined;
  if (evt.type === 'assistant/message') {
    return msg && Array.isArray(msg.content) && msg.content.length > 0 ? msg : null;
  }
  return msg && Array.isArray(msg.content) ? msg : null;
}

/** 从事件数组(可含非 surface/其它类型)折叠出当前可见 surface。 */
export function foldEvents(events: EventEnvelope[]): Surface {
  const s = newSurface();
  for (const evt of [...events].sort((a, b) => a.seq - b.seq)) {
    applyEvent(s, evt);
  }
  return s;
}

/** 折叠 seq < x 的前缀 → 该时刻的消息上下文(排他;每时每刻恢复语义)。 */
export function messagesAt(events: EventEnvelope[], x: number): Message[] {
  const s = foldEvents(events.filter((e) => e.seq < x));
  const out: Message[] = [];
  for (const seq of s.nodes) {
    const evt = s.bySeq.get(seq);
    const m = evt ? deriveMessage(evt) : null;
    if (m) out.push(m);
  }
  return out;
}

/** 最新(seq < x)的 request/header 快照;无则 null。 */
export function latestHeader(events: EventEnvelope[], x: number): EventEnvelope | null {
  let best: EventEnvelope | null = null;
  for (const e of events) {
    if (e.type !== 'request/header' || e.seq >= x) continue;
    if (best === null || e.seq > best.seq) best = e;
  }
  return best;
}
