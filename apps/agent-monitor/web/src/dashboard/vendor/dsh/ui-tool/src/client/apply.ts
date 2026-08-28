/** Register the Tool call tree, details renderer, and built-in atomic views. */
import type { RegistrationFace } from '../../../bridge/face.ts'
import type {} from '@dsh/ui-conversation'
import { ToolCallTree } from './tool/ToolCallTree.tsx'
import { ToolDetails } from './tool/ToolDetails.tsx'
import { CONVERSATION_NS as NS } from './locale.ts'
import { askQuestionToolview } from './tool/toolviews/ask-question-row.tsx'
import { bashToolviewSample } from './tool/toolviews/bash-sample.tsx'
import { fileMutationToolview } from './tool/toolviews/file-mutation-row.tsx'
import { readToolview } from './tool/toolviews/read-row.tsx'
import { searchToolview } from './tool/toolviews/search-row.tsx'
import { todoToolview } from './tool/toolviews/todo-row.tsx'
import { webToolview } from './tool/toolviews/web-row.tsx'

/**
 * Mount the whole-Tool renderers and built-in atomic Tool registrations.
 * @param face - owning registration face(gh-puller 等价 dsh Cordis ctx)。
 */
export function apply(face: RegistrationFace): void {
  const toolInject = () => ({ hooks: { hostDescription: face.hostDescription } })
  face.slots.inject('conversation.chat.node', () => face.slots.register({
    name: 'conversation.chat.node',
    key: 'tool-call',
    locale: NS,
    children: {
      'tool.call.toolview': { kind: 'keyed', scope: 'session' },
    },
    inject: toolInject,
  }, ToolCallTree))

  face.slots.inject('conversation.details.tool', () => face.slots.register({
    name: 'conversation.details.tool',
    locale: NS,
    inject: toolInject,
  }, ToolDetails))

  bashToolviewSample.apply(face)
  readToolview.apply(face)
  fileMutationToolview.apply(face)
  searchToolview.apply(face)
  webToolview.apply(face)
  todoToolview.apply(face)
  askQuestionToolview.apply(face)
}
