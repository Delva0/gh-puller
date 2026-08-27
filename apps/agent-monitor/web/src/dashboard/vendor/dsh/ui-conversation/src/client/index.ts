/**
 * ui-conversation barrel(精简):只导出契约类型面。
 * 原 dsh index.ts 会做 Cordis Context 类型增强并导出 input/service 面
 * (未 vendor);契约本身保持 dsh 原样。dsh 的 LocaleNamespaceMap 增强在
 * apply.ts(未 vendor)内,此处补齐(PropsLocale<'conversation'> 依赖)。
 */
import type { ConversationKey } from './locales.ts'

declare module '@dsh/ui-slots' {
  interface LocaleNamespaceMap {
    conversation: ConversationKey
  }
}

export type { CallId, ChatStoreState, SelectionTarget, ViewTab } from './contract/views.ts'
export type {
  AssistantChatData,
  ChatNode,
  ChatNodeDataMap,
  ChatNodeKind,
  ManualCompactionChatData,
  RetryChatData,
  ToolChatData,
  TurnTailChatData,
} from './contract/chat-nodes.ts'
export type {
  ChatFileMentions,
  ChatNodeOwnerProps,
  ChatNodeViewProps,
  ChatStore,
  ChatViewInjected,
  ChatViewSlotProps,
  CommandRowOwnerProps,
  CommandRowProps,
  ComposerBarInjected,
  ConvViewOwnerProps,
  ConvViewProps,
  ConversationInjected,
  ConversationSessionHeaderInjected,
  ConversationSessionInjected,
  ConversationSessionHeaderSlotProps,
  ConversationSessionSlotProps,
  ConversationSlotProps,
  DetailsInjected,
  DetailsSlotProps,
  DetailsToolOwnerProps,
  EmptyWorkspaceOwnerProps,
  HeroBrandMarkOwnerProps,
  MessageImagesOwnerProps,
  MessageImagesProps,
  RenderMessageImages,
  TurnTailOwnerProps,
  UseChatNodeTurnData,
} from './contract/slots.ts'
