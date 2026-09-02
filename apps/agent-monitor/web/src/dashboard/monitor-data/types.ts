/** Canonical agent event shapes shared by the monitor folds and UI adapter. */

export interface Block {
  type: string
  [key: string]: unknown
}

export interface Message {
  role: string
  content: Block[]
  [key: string]: unknown
}

export interface ModelState {
  model: string
  provider?: string
  parameters: Record<string, unknown>
}

export interface RequestState {
  model: ModelState | null
  context: Message[]
}

export interface Usage {
  input?: number
  output?: number
  cacheRead?: number
  cacheWrite?: number
  reasoning?: number
}

export interface EventEnvelope {
  seq: number
  ts: number
  session: string
  type: string
  data: Record<string, unknown>
}

export interface ModelActivity {
  requestId: string
  requestSeq: number
  responseSeq?: number
  text: string
  reasoning: string
  toolCalls: Map<number, { callId: string; name?: string; arguments: string }>
  message?: Message
  usage?: Usage
  stopReason?: string
}

export interface ToolActivity {
  callId: string
  startSeq: number
  endSeq?: number
  name?: string
  arguments?: unknown
  result?: unknown
  error?: unknown
}
