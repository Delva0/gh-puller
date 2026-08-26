/** dsh-compaction/types 增强:compaction/* 事件并入会话事件映射。 */
import type { CompactionId } from './dsh-compaction-checkpoint.ts'
import type { CommandId } from './dsh-commands.ts'
import type { ContentBlock } from '../llm/types.ts'

declare module '@dsh/session' {
  interface SessionEventMap {
    'compaction/start': { compactionId: CompactionId; sourceCommandId?: CommandId; turn: number | null }
    'compaction/summary': {
      compactionId: CompactionId
      sourceCommandId?: CommandId
      summary: ContentBlock[]
      shadowedRange: { start: number; end: number }
      shadowedSeqs: number[]
      shadowedTokenCount: number
      provider: string
      model: string
      maxTokens?: number
      usage?: { input: number; output: number; cacheRead?: number; cacheWrite?: number }
      rawOutput?: ContentBlock[]
    } & (
      | { rawOutput: ContentBlock[]; llmStreamCall: true }
      | { rawOutput?: ContentBlock[]; llmStreamCall?: never }
    )
    'compaction/end': { compactionId: CompactionId; sourceCommandId?: CommandId; turn: number | null; error?: string }
    'compaction/prune': {
      shadowedRange: { start: number; end: number }
      shadowedSeqs?: number[]
      shadowedTokenCount?: number
    }
  }
}
