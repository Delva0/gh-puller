// 单会话增量折叠引擎(live 与 history 接缝):历史页可重放,live 批次增量追加;
// 原始流高水位允许非流式历史中的 chunk seq 洞。

import type { EventEnvelope, Message } from './types';
import {
  applyEvent,
  deriveMessage,
  foldEvents,
  messagesAt as messagesAtImpl,
  newSurface,
} from './surface';

export type IngestResult = 'ok' | 'dup' | 'gap';

export class RunFold {
  private events: EventEnvelope[] = [];
  private surface = newSurface();
  private nextSeq = 0;
  private lastMessageSeq = -1;
  partial = ''; // 流式中的部分文本(未被 assistant/message 定型的 text 增量)

  /** 批量应用(历史页/补片):按 seq 键合并去重并重放,幂等;nextSeq 推进到窗口尾。 */
  applyBatch(batch: EventEnvelope[], cursor?: number): void {
    const map = new Map<number, EventEnvelope>();
    for (const e of this.events) map.set(e.seq, e);
    for (const e of batch) map.set(e.seq, e);
    this.events = [...map.values()].sort((a, b) => a.seq - b.seq);
    const eventCursor = this.events.length ? this.events[this.events.length - 1].seq + 1 : 0;
    this.nextSeq = Math.max(eventCursor, cursor ?? 0);
    this.rebuild();
  }

  /** 增量接入一批 live 尾事件;seq 洞被报告但不阻塞后续实时进度。 */
  ingestBatch(batch: EventEnvelope[]): IngestResult {
    let result: IngestResult = 'dup';
    for (const evt of [...batch].sort((a, b) => a.seq - b.seq)) {
      if (evt.seq < this.nextSeq) continue;
      if (evt.seq > this.nextSeq) result = 'gap';
      else if (result !== 'gap') result = 'ok';
      this.events.push(evt);
      this.nextSeq = evt.seq + 1;
      applyEvent(this.surface, evt);
      if (evt.type === 'assistant/message') {
        this.lastMessageSeq = evt.seq;
        this.partial = '';
      } else if (evt.type === 'assistant/chunk' && evt.seq > this.lastMessageSeq) {
        const chunk = evt.data.chunk as { type?: string; text?: string };
        if (chunk.type === 'content' || chunk.type === 'text') this.partial += chunk.text ?? '';
      }
    }
    return result;
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
    this.lastMessageSeq = msgSeq;
    let partial = '';
    for (const e of this.events) {
      if (e.type !== 'assistant/chunk') continue;
      const c = e.data.chunk as { type?: string; text?: string };
      if ((c.type === 'content' || c.type === 'text') && e.seq > msgSeq) partial += c.text ?? '';
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

  get length(): number {
    return this.nextSeq;
  }

  all(): EventEnvelope[] {
    return this.events;
  }
}
