'use client';

// 单会话响应式 store(useSyncExternalStore,不引 zustand):
// history 页/live 批次 → RunFold(seq 守卫)+ dsh 推导桥(vendor 面板数据面)。
// 旧读接口(events/chat/partial)保留兼容;面板改吃 dsh(桥)快照。
import { useSyncExternalStore } from 'react';
import { RunFold } from '../monitor-data';
import type { EventEnvelope } from '../monitor-data';
import {
  createDshSession,
  type DshSessionStore,
  type SessionFacts,
} from '../vendor/dsh/bridge/dsh-session';

interface SessionSnapshot {
  events: EventEnvelope[];
  dsh: DshSessionStore | null;
  lastSeq: number | null; // evt_ready 时 hub 报告的当前末 seq
  ready: boolean; // 收到 evt_ready(订阅窗口已登记)
  gapFrom: number | null; // 最近缺口起点(hook 据此发 history 补片)
}

class MonitorSessionStore {
  private fold = new RunFold();
  private dsh: DshSessionStore;
  private lastSeq: number | null = null;
  private ready_ = false;
  private gap_ = false;
  private version = 0;
  private listeners = new Set<() => void>();

  constructor() {
    this.dsh = createDshSession();
  }

  subscribe = (cb: () => void) => {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  };

  /** useSyncExternalStore 的快照指纹:任一变更 → 版本+1 → 重渲(数据字段渲染期直读)。 */
  getVersion = (): number => this.version;

  /** 面板宿主取桥源(装配后由 installDsh attach)。 */
  getBridge(): DshSessionStore {
    return this.dsh;
  }

  /** 装配(installDsh 内部回调):把推导注册表接入桥。 */
  wire(events: unknown, views: unknown): void {
    this.dsh.attach(events as never, views as never);
  }

  reset(facts: SessionFacts | null): void {
    this.fold = new RunFold();
    this.lastSeq = null;
    this.ready_ = false;
    this.gap_ = false;
    this.dsh.reset(facts);
    this.bump();
  }

  ready(lastSeq: number | null): void {
    this.lastSeq = lastSeq;
    this.ready_ = true;
    this.bump();
  }

  applyBatch(batch: EventEnvelope[], hasMore?: boolean): void {
    const cursor = this.lastSeq === null ? undefined : this.lastSeq + 1;
    this.fold.applyBatch(batch, cursor);
    this.gap_ = false;
    this.dsh.seed(batch, hasMore);
    this.bump();
  }

  /** live 批次;一帧内只发布一次 UI 快照。 */
  ingestBatch(events: EventEnvelope[]): 'ok' | 'dup' | 'gap' {
    const r = this.fold.ingestBatch(events);
    if (r === 'gap') this.gap_ = true;
    this.dsh.appendBatch(events);
    this.bump();
    return r;
  }

  snapshot(): SessionSnapshot {
    return {
      events: this.fold.all(),
      dsh: this.dsh,
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
