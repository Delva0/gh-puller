'use client';

// One selected session: canonical folds plus a thin DSH presentation adapter.
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
  lastSeq: number | null;
  ready: boolean;
  gapFrom: number | null;
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

  /** Version used as the useSyncExternalStore snapshot. */
  getVersion = (): number => this.version;

  /** Return the presentation adapter consumed by the existing panels. */
  getBridge(): DshSessionStore {
    return this.dsh;
  }

  /** Attach DSH presentation registries. */
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

  /** Apply one live batch and publish one UI version. */
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

// The hub exposes one selected-session subscription per viewer connection.
export const sessionStore = new MonitorSessionStore();

/** Subscribe to and return the current selected-session snapshot. */
export function useMonitorSession(): SessionSnapshot {
  useSyncExternalStore(sessionStore.subscribe, sessionStore.getVersion);
  return sessionStore.snapshot();
}
