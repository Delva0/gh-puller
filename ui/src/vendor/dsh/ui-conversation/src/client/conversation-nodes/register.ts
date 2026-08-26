import type { RegistrationFace } from '../../../../bridge/face.ts'
import { registerAssistantConversationNode } from './assistant.ts'
import { registerChatConversationView } from './chat-snapshot-builder.ts'
import { registerCommandConversationNode } from './command.ts'
import { registerCompactionConversationNode } from './compaction.ts'
import { registerUnknownConversationFallback } from './fallback.ts'
import { registerInboxConversationNodes } from './inbox.ts'
import { registerMessageConversationNode } from './message.ts'
import { registerRetryConversationNode } from './retry.ts'
import { registerToolConversationNode } from './tool.ts'
import { registerTurnErrorConversationNode } from './turn-error.ts'
import { registerTurnMaxTokensConversationNode } from './turn-max-tokens.ts'
import { registerTurnTailConversationNode } from './turn-tail.ts'

/**
 * Register the Chat business Definitions and target builder contributed by this package.
 * @param ctx - owning UI Conversation context.
 */
export function registerConversationNodes(face: RegistrationFace): void {
  registerInboxConversationNodes(face)
  registerMessageConversationNode(face)
  registerAssistantConversationNode(face)
  registerToolConversationNode(face)
  registerCommandConversationNode(face)
  registerCompactionConversationNode(face)
  registerRetryConversationNode(face)
  registerTurnErrorConversationNode(face)
  registerTurnMaxTokensConversationNode(face)
  registerTurnTailConversationNode(face)
  registerUnknownConversationFallback(face)
  registerChatConversationView(face)
}
