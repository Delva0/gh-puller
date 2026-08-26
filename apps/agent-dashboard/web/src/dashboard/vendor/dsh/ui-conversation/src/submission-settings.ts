/**
 * submission-settings(去 schemastery 适配):原 dsh 文件用 schemastery 建宿主
 * 设置 schema;gh-puller 宿主无 schemastery,保留公开常量/类型面,规则边界已有
 * composer-submission 消费(仅类型)。
 * @module ui-conversation/submission-settings
 */

export const CONVERSATION_SETTINGS_NAMESPACE = 'ui-conversation'
export const BUSY_ENTER_FIELD = 'busyEnter'
export const BUSY_ENTER_BEHAVIORS = ['queue', 'steer'] as const

export type BusyEnterBehavior = typeof BUSY_ENTER_BEHAVIORS[number]

export const DEFAULT_BUSY_ENTER_BEHAVIOR: BusyEnterBehavior = 'queue'

export interface ConversationSettings {
  busyEnter: BusyEnterBehavior
}
