/** dsh-client-locale/client 类型 shim:common 命名空间(loading 等跨特性标准词)。 */
import type { CommonKey } from './common-locale/zh.ts'

declare module '@dsh/ui-slots' {
  interface LocaleNamespaceMap {
    /** Shared cross-feature vocabulary, consulted after the entry's own namespace misses. */
    common: CommonKey
  }
}
