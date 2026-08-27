/** dsh-compaction/checkpoint 类型 shim(来源形状照 dsh 源)。 */
import type { Branded } from './dsh-brand.ts'
import type { CommandId } from './dsh-commands.ts'

export type CompactionId = Branded<'CompactionId'>

export type CompactionCheckpointSource = {
  readonly kind: 'plugin'
  readonly plugin: 'compact'
} & {
  readonly compactionId: CompactionId
  readonly sourceCommandId?: CommandId
}
