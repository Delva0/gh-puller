/** dsh-agent/types 类型 shim:inbox 事件并入会话事件映射。 */
export type InboxTarget = 'next-turn' | 'next-step'
import type { UserMessage } from '../llm/message.ts'

declare module '@dsh/session' {
  interface SessionEventMap {
    'agent/inbox/spliced': {
      target: InboxTarget
      start: number
      removedCount?: number
      inserted: UserMessage[]
      outcome?: 'canceled'
    }
  }
}
