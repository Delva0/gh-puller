'use client';

// 单会话响应式 store(useSyncExternalStore,不引 zustand):
// history 页/live evt → RunFold(seq 守卫 + 间隙可补片)→ 快照派生(对话节点/请求序)
import { useSyncExternalStore } from 'react';
import { RunFold, buildSnapshot } from '../monitor';
import type { EventEnvelope } from '../monitor';

interface SessionSnapshot {
  events: EventEnvelope[];
  chat: ReturnType<typeof buildSnapshot>;
  partial: string;
  lastSeq: number | null; // evt_ready 时 hub 报告的当前末 seq
  ready: boolean; // 收到 evt_ready(订阅窗口已登记)
  gapFrom: number | null; // 最近缺口起点(hook 据此发 history 补片)
}

class MonitorSessionStore {
  private fold = new RunFold();
  private lastSeq: number | null = null;
  private ready_ = false;
  private gap_ = false;
  private version = 0;
  private listeners = new Set<() => void>();

  subscribe = (cb: () => void) => {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  };

  /** useSyncExternalStore 的快照指纹:任一变更 → 版本+1 → 重渲(数据字段渲染期直读)。 */
  getVersion = (): number => this.version;

  reset(): void {
    this.fold = new RunFold();
    this.lastSeq = null;
    this.ready_ = false;
    this.gap_ = false;
    this.bump();
  }

  ready(lastSeq: number | null): void {
    this.lastSeq = lastSeq;
    this.ready_ = true;
    this.bump();
  }

  applyBatch(batch: EventEnvelope[]): void {
    if (!batch.length) return;
    this.fold.applyBatch(batch);
    this.gap_ = false;
    this.bump();
  }

  /** live 单条;返回 'gap' 时以 gapFrom 补片。 */
  ingest(evt: EventEnvelope): 'ok' | 'dup' | 'gap' {
    const r = this.fold.ingest(evt);
    if (r === 'gap') this.gap_ = true;
    this.bump();
    return r;
  }

  snapshot(): SessionSnapshot {
    return {
      events: this.fold.all(),
      chat: buildSnapshot(this.fold.all()),
      partial: this.fold.partial,
      lastSeq: this.lastSeq,
      ready: this.ready_,
      gapFrom: this.gap_ ? this.fold.requestedFrom() : null,
    };
  }

  private bump(): void {
    this.version += 1;
    for (const cb of this.listeners) cb();
  }
}

// 单例:一次只关注一个会话(与 hub 单订阅视图语义一致)
export const sessionStore = new MonitorSessionStore();

/** 订阅并返回当前快照(渲染期直读 store 数据)。 */
export function useMonitorSession(): SessionSnapshot {
  useSyncExternalStore(sessionStore.subscribe, sessionStore.getVersion);
  return sessionStore.snapshot();
}
