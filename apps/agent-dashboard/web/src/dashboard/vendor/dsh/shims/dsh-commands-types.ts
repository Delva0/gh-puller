/** dsh-commands/types 增强:command/* 事件并入会话事件映射。 */
import type { CommandId } from './dsh-commands.ts'

declare module '@dsh/session' {
  interface SessionEventMap {
    'command/run': { commandId: CommandId; name: string; args?: string; source: string }
    'command/done': {
      commandId: CommandId
      kind: 'success' | 'error'
      text?: string
      sourceEventSeq?: number
      reason?: string
    }
  }
}
