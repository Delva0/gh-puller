/** dsh-llm-retry/types 类型 shim(载荷形状照 dsh 源)。 */
import type { LlmFailure } from '../llm/types.ts'
import type { Branded } from './dsh-brand.ts'

export type RetryId = Branded<'RetryId'>

export type LlmRetryEventData =
  | {
    retryId: RetryId
    turn: number
    step: number
    provider: string
    mode: 'normal'
    policyKey: string
    retry: number
    maxRetries: number
    delayMs: number
    failure: LlmFailure
  }
  | {
    retryId: RetryId
    turn: number
    step: number
    provider: string
    mode: 'always'
    policyKey: string
    retry: number
    delayMs: number
    failure: LlmFailure
  }

declare module '@dsh/session' {
  interface SessionEventMap {
    'llm/retry': LlmRetryEventData
    'llm/retry-started': {
      retryId: RetryId
      turn: number
      step: number
      provider: string
      policyKey: string
      retry: number
      maxRetries?: number
      failure?: LlmFailure
    }
  }
}
