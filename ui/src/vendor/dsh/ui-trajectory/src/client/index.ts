/**
 * Browser trajectory plugin contributing one entry to the conversation view
 * slot without defining a service.
 *
 * 去 Cordis(gh-puller 内嵌):dsh `apply(ctx)`/`inject` 依赖 `ctx.sessions.binding(id).session`;
 * 本宿主由 bridge 的 DshSessionSource 提供等价 loadOlder 语义,经 RegistrationFace 注入。
 */
import type { RegistrationFace } from '../../../bridge/face.ts'
import type { SessionId } from '@dsh/runtime'
import { createTrajectoryDurationStore } from './duration-store.ts'
import { en, NS, zh } from './locales.ts'
import { registerTrajectoryAssistantDefinition } from './trajectory-assistant-definition.ts'
import { registerTrajectoryCompactionDefinitions } from './trajectory-compaction-definition.ts'
import { registerTrajectoryMessageDefinitions } from './trajectory-message-definitions.ts'
import { registerTrajectoryRequestHeaderDefinition } from './trajectory-request-header-definition.ts'
import { registerTrajectoryConversationView } from './trajectory-snapshot-builder.ts'
import { registerTrajectoryToolDefinition } from './trajectory-tool-definition.ts'
import { TrajectoryView, type TrajectoryViewInjected } from './TrajectoryView.tsx'

/** Client plugin body: register the trajectory view tab. */
export function apply(face: RegistrationFace): void {
  face.locale.register(NS, { zh, en })
  // Registration-time text (the view tab label) reads through the bound
  // translate as a thunk, so it follows the active locale without
  // re-registration.
  const t = face.locale.bind(NS)
  const duration = createTrajectoryDurationStore()
  registerTrajectoryMessageDefinitions(face)
  registerTrajectoryRequestHeaderDefinition(face)
  registerTrajectoryAssistantDefinition(face)
  registerTrajectoryToolDefinition(face)
  registerTrajectoryCompactionDefinitions(face)
  registerTrajectoryConversationView(face)
  face.slots.inject('conversation.view', () => face.slots.register({
    name: 'conversation.view',
    id: 'trajectory',
    order: 10,
    locale: NS,
    label: () => t('view.trajectory'),
    inject: (sessionId: SessionId): TrajectoryViewInjected => ({
      hooks: { duration },
      loadOlder: () => face.loadOlder(sessionId),
      setActualDuration: (value) => { duration.set(value) },
    }),
  }, TrajectoryView))
}
