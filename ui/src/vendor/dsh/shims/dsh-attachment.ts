/** dsh-attachment 类型 shim(vendor 面板引用其 id/镜像元数据)。 */
import type { Branded } from './dsh-brand.ts'

export type AttachmentId = Branded<'AttachmentId'>
export type ImageMediaType = 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif'

export interface ImageAttachmentRef {
  attachmentId: AttachmentId
  mediaType: ImageMediaType
  bytes: number
  width: number
  height: number
  name?: string
}

export interface ImageAttachmentLimits {
  maxImageBytes: number
  maxImagesPerMessage: number
  maxMessageImageBytes: number
  maxImagePixels: number
  maxImageDimension: number
  mediaTypes: readonly ImageMediaType[]
}
