/** WebSocket frames and sequence helpers for the monitor hub protocol. */

import type { EventEnvelope } from '../events/types';

export interface SessionMeta {
  session: string;
  run_id?: string | null;
  label: string;
  agent?: string;
  state: 'running' | 'completed' | 'aborted';
  ts: number;
  last_ts: number;
  num_events: number;
}

// Viewer to hub.
export type ViewerFrame =
  | { type: 'index' }
  | { type: 'history'; session: string; beforeSeq?: number; max?: number }
  | { type: 'subscribe'; session: string }
  | { type: 'delete'; session: string }
  | { type: 'ping' };

// Hub to viewer.
export type HubFrame =
  | { type: 'index'; sessions: SessionMeta[] }
  | { type: 'history'; session: string; events: EventEnvelope[]; hasMore: boolean; nextBeforeSeq: number | null }
  | { type: 'evt_ready'; session: string; lastSeq: number | null }
  | { type: 'evt'; event: EventEnvelope }
  | { type: 'evts'; events: EventEnvelope[] }
  | { type: 'pong' };

/** Return events in session sequence order. */
export function sortedEvents(events: EventEnvelope[]): EventEnvelope[] {
  return [...events].sort((a, b) => a.seq - b.seq);
}

/** Merge history and live windows by sequence number. */
export function mergeEvents(a: EventEnvelope[], b: EventEnvelope[]): EventEnvelope[] {
  const map = new Map<number, EventEnvelope>();
  for (const e of a) map.set(e.seq, e);
  for (const e of b) map.set(e.seq, e);
  return sortedEvents([...map.values()]);
}
