/**
 * 装配(gh-puller 等价 dsh 的 apply.ts):SlotRegistry + 推导注册表 + renderer +
 * locale face + host faces,按 dsh apply 顺序注册:
 *   'root'(壳条目,声明 conversation.session/header)→ 'conversation.session'
 *   (children 'conversation.view' + chatStore + inject)→ 'conversation.session.header'
 *   → 'conversation.view' id:'chat'(ChatView + ChatViewInjected)→ 节点渲染器
 *   → ui-trajectory apply(轨迹视图定义与快照定义)。
 * 未 vendor 的 dsh 外壳(composer/hero/输入/队列/详情列)一律不注册。
 */
import type { BoundActions } from '@dsh/ui-slots'
import { resolveSlotLabel } from '@dsh/ui-slots'
import {
  ConversationEventRegistry,
  ConversationViewRegistry,
  SlotRegistry,
  type ConversationRuntime,
  type SessionId,
} from '@dsh/runtime'
import { createSlotRenderer } from '../ui-renderer/src/client/scoped-slots.tsx'
import type { DshSessionStore } from './dsh-session'
import { createDshLocaleFace, registerDshDict, setDshLocale } from './locale'
import type { RegistrationFace } from './face'
import {
  ConversationSession,
  ConversationSessionHeader,
} from '../ui-conversation/src/client/skeleton/ConversationSession'
import { ChatView } from '../ui-conversation/src/client/chat/ChatView'
import { registerChatNodeRenderers } from '../ui-conversation/src/client/chat/register-node-renderers'
import { registerConversationNodes } from '../ui-conversation/src/client/conversation-nodes/register'
import { createChatStore } from '../ui-conversation/src/client/stores'
import type { ChatViewInjected } from '../ui-conversation/src/client/contract/slots'
import { NS as CONV_NS, zh as convZh, en as convEn } from '../ui-conversation/src/client/locales'
import { apply as applyTrajectory } from '../ui-trajectory/src/client'

export interface DshInstall {
  registry: SlotRegistry
  events: ConversationEventRegistry
  views: ConversationViewRegistry
  /** 视图标签 ledger(对话/轨迹 tabs)。 */
  ledger: {
    list: () => Array<{ id: string; label: string }>
    subscribe: (fn: () => void) => () => void
    version: () => number
  }
  runtime: ConversationRuntime
  setLocale: (lang: string | null | undefined) => void
}

/** 根条目壳组件(DshPanels 提供;入参字段形状见 DshShellProps)。 */
export type DshShellComponent = (props: {
  useSessions: <S>(sel: (s: unknown) => S) => S
  renderSlot: (key: string, owner: object, opts?: { only?: string }) => unknown
}) => unknown

const DSH_NS = ['conversation', 'trajectory']

/** 类型擦除的 register(桥接层胶水):SlotCore 重载按 SlotMap 行建模,
 * root/会话条目的 children 面在 dsh 由 apply 层提供,此处统一化。 */
function registerSlot(registry: SlotRegistry, options: object, component: unknown): void {
  void (registry as unknown as { register(opts: object, comp: unknown): () => void })
    .register(options, component)
}

function installOnce(store: DshSessionStore, shell: DshShellComponent): DshInstall {
  const registry = new SlotRegistry()
  const events = new ConversationEventRegistry()
  const views = new ConversationViewRegistry()
  const localeFace = createDshLocaleFace()
  registry.installLocale(localeFace)
  registry.install(createSlotRenderer())

  registerDshDict(CONV_NS, convZh, convEn)
  const face: RegistrationFace = {
    conversationEvents: events,
    conversationViews: views,
    slots: registry,
    locale: {
      register: (ns, dicts) => {
        registerDshDict(ns, dicts.zh ?? {}, dicts.en ?? {})
      },
      bind: ns => localeFace.bind(ns),
    },
    loadOlder: sessionId => store.loadOlder(sessionId),
  }
  applyTrajectory(face)

  const tChat = localeFace.bind(CONV_NS)

  /** chat.node 声明级 inject(dsh apply 原样):turnData 钩子工厂。
   * (standard.useSession + hookContext=nodeKey → 读取该节点 location 的 turn 数据) */
  const CHAT_NODE_INJECT = {
    hooks: {
      turnData: (({ useSession }: { useSession: <T>(sel: (s: unknown) => T) => T }, nodeKey: string) =>
        function useTurnData(key: string) {
          return useSession((snapshot) => {
            const location = (snapshot as { chat: { nodes: { get(k: string): { location?: { kind?: string; turn?: { data: Map<string, unknown> } } } | undefined } } })
              .chat.nodes.get(nodeKey)?.location
            return location?.kind === 'turn' || location?.kind === 'step'
              ? location.turn?.data.get(key)
              : undefined
          })
        }),
    },
  }
  const chatStore = createChatStore()
  const ledger = {
    list() {
      const tabs: Array<{ id: string; label: string }> = []
      for (const entry of registry.entries('conversation.view')) {
        const id = entry.options.id
        if (id === undefined) continue
        tabs.push({ id, label: resolveSlotLabel(entry.options.label) ?? id })
      }
      return tabs
    },
    subscribe: (fn: () => void) => registry.subscribe('conversation.view', fn),
    version: () => registry.getVersion('conversation.view'),
  }
  const chatScrollPositions = new Map<string, unknown>()

  // 'root':壳条目(children 声明 header/session;dsh 的 root 由 ui-layout AppFrame 占据,
  // gh-puller 只渲染对话/轨迹两面板,故注入我们自己的壳组件)。
  registerSlot(registry, {
    name: 'root',
    children: {
      'conversation.session': { kind: 'single', scope: 'session' },
      'conversation.session.header': { kind: 'single', scope: 'session' },
    },
  }, shell)

  registerSlot(registry, {
    name: 'conversation.session',
    children: { 'conversation.view': { kind: 'list', scope: 'session' } },
    store: chatStore,
    inject: (): unknown => ({
      views: ledger,
      releaseSessionImages: () => {},
      bindDraftMirror: () => () => {},
    }),
  }, ConversationSession as never)

  registerSlot(registry, {
    name: 'conversation.session.header',
    locale: CONV_NS,
    children: {
      'conversation.session.header.actions': { kind: 'list', scope: 'session' },
      'conversation.session.header.utilities': { kind: 'list', scope: 'session' },
    },
    store: chatStore,
    inject: (): unknown => ({
      views: ledger,
      open: () => {},
    }),
  }, ConversationSessionHeader as never)

  // 对话视图条目(ChatView;dsh apply 原文注入面,gh-puller 惰性面)。
  registerSlot(registry, {
    name: 'conversation.view',
    id: 'chat',
    order: 0,
    label: () => tChat('view.chat'),
    locale: CONV_NS,
    children: {
      'conversation.chat.node': { kind: 'keyed', scope: 'session', inject: CHAT_NODE_INJECT },
      'conversation.message.images': { kind: 'single', scope: 'session' },
    },
    store: chatStore,
    inject: (sessionId: SessionId, actions: BoundActions<typeof chatStore>): ChatViewInjected => ({
      openDetails: target => { actions.select(target) },
      fileMentions: () => undefined,
      openFile: async () => {},
      loadOlder: async () => { await store.loadOlder(sessionId) },
      loadImage: async () => '',
      inspectCall: callId => {
        actions.setInspect({ callId })
        actions.setView('trajectory')
      },
      chatScroll: {
        save: position => {
          if (position === null) chatScrollPositions.delete(sessionId)
          else chatScrollPositions.set(sessionId, position)
        },
        read: () => (chatScrollPositions.get(sessionId) ?? null) as never,
      },
      forkAt: () => {},
    }),
  }, ChatView as never)

  registerConversationNodes(face)
  registerChatNodeRenderers(face)

  registry.setHostFaces({
    sessions: { list: store.list, currentProvideInfo: store.provideInfo },
    workspaces: {
      list: { getSnapshot: () => ({ phase: 'ready' as const, items: [] }), subscribe: () => () => {} },
    },
  })

  // 桥接入推导注册表(此后事件窗折叠产出真实快照)
  store.attach(events, views)

  void DSH_NS
  return {
    registry,
    events,
    views,
    ledger,
    runtime: { events: events as never, views: views as never },
    setLocale: setDshLocale,
  }
}

let installed: DshInstall | null = null

/** 安装(幂等);shell 为根条目壳组件。 */
export function installDsh(store: DshSessionStore, shell: DshShellComponent): DshInstall {
  if (installed === null) {
    installed = installOnce(store, shell)
  }
  return installed
}

export function getDshInstall(): DshInstall {
  if (installed === null) throw new Error('installDsh() 尚未调用')
  return installed
}
