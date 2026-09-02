/** Canonical agent event shapes consumed by the monitor fold and native UI. */

export const OPAQUE = '<opaque>';

export interface ContentPart {
  type: string
  [key: string]: unknown
}

export interface Item {
  type: string
  role?: string
  content?: ContentPart[]
  call_id?: string
  name?: string
  arguments?: string
  output?: unknown
  [key: string]: unknown
}

export interface AgentState {
  agent: string | null
  config: Record<string, unknown>
}

export interface CanonicalState {
  agent: AgentState | null
  context: Item[]
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
  output?: Item[]
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
