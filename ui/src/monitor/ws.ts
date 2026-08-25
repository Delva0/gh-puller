// hub 线协议帧的 TS 类型与客户端补片助手(仿 dsh apiproxy/api/events.ts 的帧形态;
// gh-puller 为下行 WS + HTTP fallback 同构,帧由 hub.py 定义)。

import type { EventEnvelope } from './types';

export interface SessionMeta {
  session: string;
  run_id?: string | null;
  label: string;
  provider?: string;
  model?: string;
  state: 'running' | 'completed' | 'aborted';
  ts: number;
  last_ts: number;
  num_events: number;
}

// 查看端(→ hub)
export type ViewerFrame =
  | { type: 'index' }
  | { type: 'history'; session: string; beforeSeq?: number; max?: number }
  | { type: 'subscribe'; session: string }
  | { type: 'ping' };

// hub(→ 查看端)
export type HubFrame =
  | { type: 'index'; sessions: SessionMeta[] }
  | { type: 'history'; session: string; events: EventEnvelope[]; hasMore: boolean; nextBeforeSeq: number | null }
  | { type: 'evt_ready'; session: string; lastSeq: number | null }
  | { type: 'evt'; event: EventEnvelope }
  | { type: 'pong' };

/** 事件按 seq 升序(与 fold 的期待一致);帧解析后先调用。 */
export function sortedEvents(events: EventEnvelope[]): EventEnvelope[] {
  return [...events].sort((a, b) => a.seq - b.seq);
}

/** 合并两段(历史页 + live 缓冲)按 seq 去重;gap 检测由 RunFold.ingest 完成。 */
export function mergeEvents(a: EventEnvelope[], b: EventEnvelope[]): EventEnvelope[] {
  const map = new Map<number, EventEnvelope>();
  for (const e of a) map.set(e.seq, e);
  for (const e of b) map.set(e.seq, e);
  return sortedEvents([...map.values()]);
}
