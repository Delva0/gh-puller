// 单会话增量折叠引擎(live 与 history 接缝):seq 顺序守卫 + 间隙信号 + 可重放折叠。
// 用法:历史页先 applyBatch;之后每条 live 经 ingest;间隙(seq > 期望)时以
// requestedFrom() 向 hub 发 history(beforeSeq) 补片,再 applyBatch 重放。

import type { EventEnvelope, Message } from './types';
import { deriveMessage, foldEvents, latestHeader, messagesAt as messagesAtImpl, newSurface } from './surface';

export type IngestResult = 'ok' | 'dup' | 'gap';

export class RunFold {
  private events: EventEnvelope[] = [];
  private surface = newSurface();
  private nextSeq = 0;
  partial = ''; // 流式中的部分文本(未被 assistant/message 定型的 text 增量)

  /** 批量应用(历史页/补片):按 seq 键合并去重并重放,幂等;nextSeq 推进到窗口尾。 */
  applyBatch(batch: EventEnvelope[]): void {
    const map = new Map<number, EventEnvelope>();
    for (const e of this.events) map.set(e.seq, e);
    for (const e of batch) map.set(e.seq, e);
    this.events = [...map.values()].sort((a, b) => a.seq - b.seq);
    this.nextSeq = this.events.length ? this.events[this.events.length - 1].seq + 1 : 0;
    this.rebuild();
  }

  /** live 单条:seq 顺序守卫。返回 'gap' 时以 requestedFrom() 为缺口起点补片。 */
  ingest(evt: EventEnvelope): IngestResult {
    if (evt.seq < this.nextSeq) return 'dup';
    if (evt.seq > this.nextSeq) return 'gap';
    this.events.push(evt);
    this.nextSeq += 1;
    this.rebuild();
    return 'ok';
  }

  requestedFrom(): number {
    return this.nextSeq;
  }

  private rebuild(): void {
    this.surface = foldEvents(this.events);
    // partial:最后的 assistant/message 之后的 text 增量(尚未定型)
    let msgSeq = -1;
    for (const e of this.events) {
      if (e.type === 'assistant/message') msgSeq = e.seq;
    }
    let partial = '';
    for (const e of this.events) {
      if (e.type !== 'assistant/chunk') continue;
      const c = e.data.chunk as { type?: string; text?: string };
      if (c.type === 'text' && e.seq > msgSeq) partial += c.text ?? '';
    }
    this.partial = partial;
  }

  /** 当前可见消息上下文(折叠全量)。 */
  messages(): Message[] {
    const out: Message[] = [];
    for (const seq of this.surface.nodes) {
      const evt = this.surface.bySeq.get(seq);
      const m = evt ? deriveMessage(evt) : null;
      if (m) out.push(m);
    }
    return out;
  }

  /** 任意时刻 x(seq 排他)的消息上下文(来自已接收窗口)。 */
  messagesAt(x: number): Message[] {
    return this.events.length ? messagesAtImpl(this.events, x) : [];
  }

  /** 最新请求头快照(seq < x;x 缺省取全量尾部)。 */
  headerAt(x = Infinity): EventEnvelope | null {
    return this.events.length ? latestHeader(this.events, x) : null;
  }

  get length(): number {
    return this.nextSeq;
  }

  all(): EventEnvelope[] {
    return this.events;
  }
}
