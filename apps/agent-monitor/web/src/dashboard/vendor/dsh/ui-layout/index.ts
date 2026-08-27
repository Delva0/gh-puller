/**
 * ui-layout 缩小版(类型面):只保留对话/轨迹面板所需的两个 SlotMap 行。
 * dsh 的 AppFrame/sidebar 列不在 gh-puller 宿主内,此处仅声明行与该包
 * 自身的 owner 契约(无会话外壳,owner 面为空),类型与原 dsh 一致。
 */
export interface ConvOwnerProps {}
export interface DetailsOwnerProps {}

declare module '@dsh/ui-slots' {
  interface SlotMap {
    /** The whole center column (dsh: ui-conversation ConversationRoot;gh-puller 不占此座,类型保留)。 */
    'conversation': { kind: 'single'; scope: 'session-maybe'; owner: ConvOwnerProps }
    /** The right details column(dsh: DetailsPanel;gh-puller 不渲染 details 列,类型保留)。 */
    'details': { kind: 'single'; scope: 'session'; owner: DetailsOwnerProps }
  }
}
