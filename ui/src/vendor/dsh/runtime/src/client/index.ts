/**
 * runtime barrel(精简版):只导出 vendor 面板/桥接层所需的符号面。
 *
 * 原 dsh barrel(runtime/src/client/index.ts)会经 Cordis 服务把
 * workspaces/sessions-service/manager/api-remotes 整棵树拖进编译面;
 * gh-puller 不装运行时容器,这些服务由 bridge 层承担,故 barrel 收窄。
 * 各子模块推导/类型逻辑保持 dsh 原样不动。
 *
 * 补充的本地类型(SessionId/SessionListState/SessionSummary)是
 * ConversationSession 面包屑/哑选择器所需的最小形状(对比
 * sessions/service.ts 的 SessionListState)。
 */
import type { SessionId } from '../../../shims/dsh-api-remotes.ts'
import type { SessionProjectionMap } from '@dsh/session-projection'
import type { Branded } from '../../../shims/dsh-brand.ts'

export type { SessionId } from '../../../shims/dsh-api-remotes.ts'
import type { ConversationSnapshot } from './sessions/conversation.ts'

/** 原 runtime barrel 的会话域最小面(queue.ts 只取 updateQueue 参数类型)。 */
export interface SessionFace {
  readonly updateQueue: (item: unknown, action: unknown) => void
}

/** Client 域上下文(原 dsh barrel:export type ClientContext = Context)。 */
export type ClientContext = import('../../../shims/cordis.ts').Context

/** 工作区 id(原 dsh workspaces/service);gh-puller 无工作区,品牌类型保留。 */
export type WorkspaceId = Branded<'WorkspaceId'>
import type {
  HostObservable,
  MaybeSnapshotSelectorHook,
  SessionMaybeProvideInfo,
  SnapshotSelectorHook,
} from '@dsh/ui-slots'

/** 投影选择器钩子(原 runtime projection-store;key 映射由 shims 增强,同 dsh)。 */
export type UseProjection = {
  <K extends Extract<keyof SessionProjectionMap, string>>(key: K): SessionProjectionMap[K] | undefined
  <K extends Extract<keyof SessionProjectionMap, string>, S>(
    key: K,
    selector: (value: SessionProjectionMap[K] | undefined) => S,
    eq?: (a: S, b: S) => boolean,
  ): S
}

/** Workspace 列表最小形状(原 runtime barrel 经 workspaces/service.ts 引用)。 */
export interface WorkspaceListState {
  readonly phase: 'loading' | 'ready' | 'error' | 'empty'
  readonly items: readonly { readonly workspaceId: string; readonly title: string; readonly sessionIds: readonly string[] }[]
}

declare module '@dsh/ui-slots' {
  /**
   * Session standard kit, real members (ui-slots declares the empty seat;
   * the runtime — where the subjects live — merges the concrete types):
   * every session-scope slot component receives these from the framework.
   */
  interface SessionStandardProps {
    useSession: SnapshotSelectorHook<ConversationSnapshot>
    /** The framework-resolved session id (owners never pass it). */
    sessionId: SessionId
    /** The fifth framework hook seat: key-addressed projection reader (undefined = capability absent). */
    useProjection: UseProjection
  }
  /** Standard kit for slots that remain mounted while current session changes. */
  interface SessionMaybeStandardProps {
    useSession: MaybeSnapshotSelectorHook<ConversationSnapshot>
    /** Current session id; absent in the no-session state. */
    sessionId: SessionId | undefined
    /** Key-addressed projection reader; every key reads absent while no session is current. */
    useProjection: UseProjection
  }
  /** Props injected into every global slot component. */
  interface GlobalStandardProps {
    useSessions: SnapshotSelectorHook<SessionListState>
    useWorkspaces: SnapshotSelectorHook<WorkspaceListState>
  }
}

export { SlotRegistry, type RootOwnerProps } from './slots.ts'
export { ConversationEventRegistry } from './conversation/event-registry.ts'
export { ConversationViewRegistry } from './conversation/view-registry.ts'
export {
  ConversationNodeAssembler,
  type ConversationEventDefinitions,
  type ConversationRuntime,
  type ConversationViewDefinitions,
} from './sessions/conversation-assembler.ts'
export { ConversationLocationIndex } from './sessions/conversation-location-index.ts'
export * from './contract/conversation.ts'
export {
  createSnapshotStore,
  defineStore,
  shallowEqual,
  type EngineStoreHandle,
  type EngineStoreInstance,
  type ObservableSnapshot,
  type SnapshotStore,
} from './contract/store.ts'
export { emptyAssistantBlock } from './sessions/partial.ts'
export * from './sessions/assistant-timing.ts'
export {
  contextForm,
  contextProvenance,
  sessionRecallLabels,
  type KnownContextForm,
  type ContextProvenanceView,
} from './sessions/context-provenance.ts'
export { displayFailureMessage } from './sessions/failure-display.ts'
export {
  type ConversationPromptSnapshot,
  type RequestInspectionSnapshot,
  type RequestPromptChange,
  type RequestView,
} from './sessions/request-inspection.ts'
export { type PendingInteraction, type PendingWait } from './sessions/pending.ts'
export { isAppendSurfaceEvent, isReplacementSurfaceEvent } from '../../../session/surface.ts'
export * from './sessions/conversation.ts'

/** gh-puller 侧会话列表的最小形状(与 hub SessionMeta 对齐,供面包屑派生)。 */
export interface SessionSummary {
  readonly id: SessionId
  readonly displayTitle: string
  readonly cwd?: string
  readonly origin?: 'session' | 'subagent'
  readonly parentId?: SessionId
}

export interface SessionListState {
  readonly byId: Record<SessionId, SessionSummary>
  readonly phase: 'loading' | 'ready' | 'error' | 'empty'
}
