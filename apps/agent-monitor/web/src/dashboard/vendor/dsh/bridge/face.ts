/**
 * 桥接层面:vendor 注册尾函数的替代依赖(dsh 原为 Cordis `Context`)。
 * gh-puller 宿主不装 Cordis,注册函数统一收 `RegistrationFace`。
 */
import type { ConversationEventRegistry, ConversationViewRegistry } from '@dsh/runtime'
import type { SlotRegistry } from '../runtime/src/client/slots.ts'
import type { HostDescriptionSource } from '../shims/dsh-client-connection.ts'

/** 注册上下文面:替换 dsh 的 Cordis ctx(conversationEvents/slots/locale…)。 */
export interface RegistrationFace {
  conversationEvents: ConversationEventRegistry
  conversationViews: ConversationViewRegistry
  slots: SlotRegistry
  locale: {
    register(ns: string, dicts: Record<string, Record<string, string>>): void
    bind(ns: string): (key: string, params?: Record<string, unknown>) => string
  }
  /** Host 描述源(ui-tool 工具行 `~` 缩短;hub 协议未带 home 时为静态空源)。 */
  hostDescription: HostDescriptionSource
  /**
   * gh-puller 视角的"加载更早历史":请求 history 前页并重折叠轨迹快照,
   * 返回轨迹快照是否变化(等价 dsh `session.loadOlder()` 的语义)。
   */
  loadOlder(sessionId: string): Promise<boolean>
}
