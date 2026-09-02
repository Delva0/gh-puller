/** Canonical agent event shapes consumed by the monitor fold and native UI. */

export interface Block {
  type: string
  [key: string]: unknown
}

export interface Message {
  role: string
  content: Block[]
  [key: string]: unknown
}

export interface AgentState {
  agent: string | null
  config: Record<string, unknown>
}

export interface CanonicalState {
  agent: AgentState | null
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
  request: Record<string, unknown>
  stateAtRequest: CanonicalState
  responseSeq?: number
  text: string
  reasoning: string
  deltaCount: number
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
