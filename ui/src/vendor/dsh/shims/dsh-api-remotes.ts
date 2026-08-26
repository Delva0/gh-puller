/** dsh-api-remotes/client 类型 shim(vendor 面板只用其会话/视图契约类型)。 */
import type { Branded } from './dsh-brand.ts'

export type SessionId = Branded<'SessionId'>

/** 父子会话寻址(子代理目录);vendor 仅类型使用,不触传输。 */
export interface SubagentAddress {
  readonly parentSessionId: SessionId
  readonly childSessionId: SessionId
  readonly mode: 'one-shot' | 'continuable'
}

/** RPC 错误占位(仅类型/异常基类用途)。 */
export class RpcError extends Error {
  readonly code: string
  constructor(code: string, message?: string) {
    super(message ?? code)
    this.name = 'RpcError'
    this.code = code
  }
}

/** 连接句柄占位(vendor 不调 connection)。 */
export interface ConnectionHandle {
  readonly api: unknown
  readonly isLoopback: boolean
  start(sinks: unknown, config?: unknown): { stop(): void }
}

export interface ToolCallView {
  name?: string
  arguments?: unknown
  previewMarkdown?: string
}
export interface ToolResultView {
  isError?: boolean
  message?: string
  previewMarkdown?: string
}
export type ToolEventView =
  | { for: 'call'; view: ToolCallView }
  | { for: 'result'; view: ToolResultView }

/** RPC 帧面(pending 载体用):与 dsh apiproxy MuxFrame 同形的最小子集。 */
export type RpcId = Branded<'RpcId'>

export interface RpcReceipt {
  readonly rpcId: RpcId
  readonly accepted: boolean
  readonly reason?: string
}

export interface ClientResponse {
  readonly type?: string
  readonly rpcId?: RpcId
  [key: string]: unknown
}

export type MuxFrame =
  | { readonly type: 'session/event'; readonly sessionId: SessionId; readonly event: unknown; readonly view?: ToolEventView }
  | { readonly type: 'session/subscribed'; readonly sessionId: SessionId; readonly lastSeq: number }
  | { readonly type: 'approval/requested'; readonly sessionId: SessionId; readonly approvalId: string; readonly toolName: string; readonly callId?: string; readonly reason?: string }
  | { readonly type: 'question/requested'; readonly sessionId: SessionId; readonly questions: readonly { id: string; message: unknown; options?: readonly { id: string; label: string }[] }[] }

