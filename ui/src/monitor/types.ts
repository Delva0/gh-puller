// 监控事件溯源模型的 TS 类型(与 gh_puller/agent/events.py 的规范同构;纯数据,零依赖)
// 折叠恢复规范:surface 节点(带 surfaceOp 的用户/助手/工具结果消息)按 seq 升序重放,
// 任意时刻的 messages = 折叠 seq<x 的前缀并派生(见 surface.ts)。

/** 消息块(text/thinking/tool_use/tool_result;与 Python 侧 data.message.content 同形)。 */
export type Block =
  | { type: 'text'; text: string }
  | { type: 'thinking'; thinking: string }
  | { type: 'tool_use'; id?: string; name?: string; input?: unknown }
  | { type: 'tool_result'; tool_use_id?: string; content?: string; is_error?: boolean }
  | { [key: string]: unknown };

export interface Message {
  role: 'user' | 'assistant';
  content: Block[];
}

export type SurfaceOp = 'append' | { op: 'replace'; start: number; end: number };

export interface SourceInfo {
  kind: 'user' | 'context';
  form?: string; // instructions | notice | snapshot | catalog
  label?: string;
}

/** 事件信封(顶层);seq 每 session 从 0 连续。 */
export interface EventEnvelope {
  id: string;
  seq: number;
  ts: number;
  session: string;
  run_id?: string | null;
  label?: string;
  provider?: string;
  model?: string;
  type: string;
  data: Record<string, unknown>;
  ignorable?: boolean;
}

// ---- 常见事件 data 载荷(窄化用) ----

export interface MessageData {
  message: Message;
  source?: SourceInfo;
  surfaceOp: SurfaceOp;
  sourceSeqs?: number[];
  turn?: number;
  step?: number;
  usage?: Usage | null;
  stop_reason?: string | null;
  interrupted?: boolean;
}

export interface ChunkData {
  turn?: number;
  step?: number;
  chunk: { type: 'text' | 'thinking' | 'tool_input'; index: number; text?: string; partial_json?: string };
}

export interface ToolCallData {
  callId: string;
  name?: string;
  arguments: string; // 原始 JSON 字符串
  turn?: number;
  step?: number;
}

export interface ToolResultData {
  callId?: string;
  name?: string;
  is_error?: boolean;
  message: Message;
  surfaceOp: SurfaceOp;
  sourceSeqs?: number[];
  turn?: number;
  step?: number;
}

export interface HeaderData {
  header: {
    config?: Record<string, unknown>;
    system?: string | null;
    tools?: Array<{ name?: string; description?: string; input_schema?: unknown }>;
  };
  reason: 'initial' | 'resume' | 'change';
  partial?: boolean;
}

export interface ContextInjectData {
  target: string;
  phase?: string;
  provenance?: string;
  text: string;
}

export interface ContextModifyData {
  target: string;
  kind: 'trim' | 'replace' | 'degrade';
  cause?: string;
  detail?: string;
  removed?: { n_turns?: number; chars?: number; est_tokens?: number };
}

export interface SessionStartData {
  run_id?: string | null;
  label?: string;
  provider?: string;
  model?: string;
  retry?: { attempt: number; prev_error?: string; prev_run?: string };
  meta?: Record<string, unknown>;
}

export interface SessionEndData {
  state: 'completed' | 'aborted';
  ok: boolean;
  duration_ms?: number;
  text_chars?: number;
  num_steps?: number;
  usage?: Usage | null;
  stop_reason?: string | null;
  reason?: string;
  total_cost_usd?: number | null;
}

export interface Usage {
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_input_tokens?: number | null;
}

export interface ErrorData {
  stage?: string;
  exc_type?: string;
  message: string;
}

// ---- 界面数据(折叠产物;组件消费) ----

export interface ChatNode {
  seq: number; // 锚定事件 seq(渲染去重/滚动定位)
  kind: 'user' | 'assistant' | 'tool' | 'tool-result' | 'context' | 'system' | 'turn-tail';
  turn: number;
  step: number;
  message?: Message;
  callId?: string;
  name?: string;
  partial?: string; // 流式中的部分文本
  contextText?: string;
  contextKind?: string;
  header?: HeaderData['header'];
  retry?: SessionStartData['retry'];
}

export interface ToolCallView {
  callId: string;
  name?: string;
  arguments?: string;
  seq: number;
  step: number;
  resultSeq?: number;
  result?: string;
  isError?: boolean;
}

export interface RequestView {
  seq: number; // 该 step 首个 chunk 的 seq(请求平面)
  step: number;
  turn: number;
  ts?: number;
  durationMs?: number;
  usage?: Usage | null;
  stopReason?: string | null;
  text: string;
  thinking: string;
  tools: ToolCallView[];
  error?: string;
  interrupted?: boolean;
  retry?: SessionStartData['retry'];
}

export interface Snapshot {
  chatNodes: ChatNode[];
  requests: RequestView[]; // 按 seq 升序
  runningCalls: ToolCallView[];
  partial: string; // 尾部流式中的未定型文本(最后一条 assistant/message 之后)
}
