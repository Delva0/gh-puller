// hub 协议帧与 LLM 流行类型(镜像 gh_puller/agent/sinks.py 的 WsSink 转发帧与 events.py 的 LLM_STREAM_TYPES)
export type SessionState = 'running' | 'completed' | 'aborted';

export interface SessionMeta {
  session: string;
  label: string;
  provider: string;
  model: string;
  state: SessionState;
  ts: number;
  last_ts: number;
}

// LLM 流行(聚合产物行;未列出的实例字段一律不读取,结构对齐聚合器)
export type LlmLine =
  | { type: 'session.start'; ts?: number; session: string; label: string; provider: string; model: string; state: string; meta?: Record<string, unknown> }
  | { type: 'round.start'; ts?: number; round: number; input_kind: string; input_preview?: string }
  | { type: 'block.start'; ts?: number; round: number; seq: number; block_type: 'thinking' | 'tool_use' | 'content' | string; tool_id?: string | null; tool_name?: string | null }
  | { type: 'block.delta'; ts?: number; round: number; seq: number; text: string }
  | { type: 'block.end'; ts?: number; round: number; seq: number; block_type?: string | null; tool_input?: unknown }
  | { type: 'tool.result'; ts?: number; round: number; tool_name?: string | null; tool_id?: string | null; is_error: boolean; content_chars: number; content_preview: string }
  | { type: 'round.end'; ts?: number; round: number }
  | { type: 'session.end'; ts?: number; state: string; duration_ms?: number | null; text_chars?: number | null; num_rounds?: number | null; usage?: Record<string, unknown> | null; reason?: string };

// 事件 kind 全集(镜像 gh_puller/agent/events.py 的 KINDS;事件流视图筛选用)
export const EVENT_KINDS = [
  'run.start', 'block.start', 'text.delta', 'thinking.delta', 'block.stop',
  'tool.use', 'tool.result', 'message.assistant', 'result', 'error', 'run.end',
] as const;

// WS 帧(查看端)
export type MonitorFrame =
  | { type: 'index'; sessions: SessionMeta[] }
  | { type: 'llm'; session: string; id: number; line: LlmLine }
  | { type: 'llm_ready'; session: string }
  | { type: 'evt'; session: string; event: Record<string, unknown> }
  | { type: 'evt_ready'; session: string }
  | { type: 'pong' };
