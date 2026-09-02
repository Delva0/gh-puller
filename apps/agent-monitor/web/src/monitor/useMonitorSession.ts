'use client';

/** Own the selected session's sole canonical fold. */
import { useSyncExternalStore } from 'react';
import { RunFold } from '../events/fold';
import type {
  EventEnvelope,
  ModelActivity,
  CanonicalState,
  ToolActivity,
} from '../events/types';

export interface SessionSnapshot {
  loaded: boolean;
  events: EventEnvelope[];
  state: CanonicalState;
  requests: ModelActivity[];
  tools: ToolActivity[];
  activeModels: ModelActivity[];
  steps: number;
}

class MonitorSessionStore {
  private fold = new RunFold();
  private lastSeq: number | null = null;
  private loaded = false;
  private version = 0;
  private listeners = new Set<() => void>();

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getVersion = (): number => this.version;

  reset(): void {
    this.fold = new RunFold();
    this.lastSeq = null;
    this.loaded = false;
    this.publish();
  }

  ready(lastSeq: number | null): void {
    this.lastSeq = lastSeq;
  }

  applyBatch(batch: EventEnvelope[]): void {
    const cursor = this.lastSeq === null ? undefined : this.lastSeq + 1;
    this.fold.applyBatch(batch, cursor);
    this.loaded = true;
    this.publish();
  }

  ingestBatch(events: EventEnvelope[]): 'ok' | 'dup' | 'gap' {
    const result = this.fold.ingestBatch(events);
    if (result === 'ok') this.publish();
    return result;
  }

  snapshot(): SessionSnapshot {
    const requests = this.fold.modelActivity();
    return {
      loaded: this.loaded,
      events: this.fold.all(),
      state: this.fold.state(),
      requests,
      tools: this.fold.toolActivity(),
      activeModels: this.fold.activeModels(requests),
      steps: this.fold.stepCount(),
    };
  }

  private publish(): void {
    this.version += 1;
    for (const listener of this.listeners) listener();
  }
}

export const sessionStore = new MonitorSessionStore();

/** Subscribe to the current selected-session fold. */
export function useMonitorSession(): SessionSnapshot {
  useSyncExternalStore(sessionStore.subscribe, sessionStore.getVersion);
  return sessionStore.snapshot();
}
