/** dsh-client-connection/client 类型 shim。 */
import type { Branded } from './dsh-brand.ts'

export type MessageId = Branded<'MessageId'>

/** Host 描述(上游 host.describe 响应面的裁剪态):POSIX `~` 缩短仅用 home。 */
export interface HostDescription {
  home?: string
  [key: string]: unknown
}

/** Host 描述的可订阅源(dsh connection.hostDescription 同形)。 */
export interface HostDescriptionSource {
  getSnapshot(): HostDescription | undefined
  subscribe(listener: () => void): () => void
}
