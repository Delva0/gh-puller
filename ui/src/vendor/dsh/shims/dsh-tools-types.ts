/** dsh-tools/types 增强:tool/code-dispatch* 事件并入会话事件映射。 */
import type { CallId } from '../llm/brand.ts'
import type { ContentBlock } from '../llm/types.ts'

declare module '@dsh/session' {
  interface SessionEventMap {
    'tool/code-dispatch-start': {
      rootCallId: CallId
      parentCallId: CallId
      subCallId: CallId
      name: string
      arguments: unknown
    }
    'tool/code-dispatch': {
      rootCallId: CallId
      parentCallId: CallId
      subCallId: CallId
      name: string
      arguments: unknown
      isError: boolean
      content: ContentBlock[]
    }
  }
}
