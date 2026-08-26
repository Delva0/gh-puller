/**
 * 会话桥:gh 事件窗(seq 守卫由连接层负责)→ dsh 推导(assembler + 视图定义)→
 * ConversationSnapshot/HostObservable 面。被 useMonitorSession(既有连接层)
 * 与 install/DshPanels 共用。
 */
import type { EventEnvelope } from '../../../monitor/types'
import type { ConversationSnapshot } from '@dsh/runtime'
import {
  ConversationNodeAssembler,
  EMPTY_CHAT_SNAPSHOT,
  type ConversationEventDefinitions,
  type ConversationEventInput,
  type ConversationViewDefinitions,
} from '@dsh/runtime'
import { GhToDshEvents } from './dsh-events'
import { createObservable, createVersioned, type HostObservable } from './observable'

export interface SessionFacts {
  sessionId: string
  label: string
  runId?: string | null
  provider?: string
  model?: string
  generator?: string
  state: 'running' | 'completed' | 'aborted'
}

/** 会话行(面包屑/状态栏用)。 */
export interface SessionRow {
  id: string
  displayTitle: string
  origin?: 'session' | 'subagent'
  parentId?: string
  cwd?: string
  state?: string
  last_ts?: number
  run_id?: string | null
  provider?: string
  model?: string
  generator?: string
  num_events?: number
}

export interface SessionListShape {
  byId: Record<string, SessionRow>
  phase: 'loading' | 'ready' | 'empty' | 'error'
}


export interface DshSessionStore {
  reset(facts: SessionFacts | null): void
  /** 已在连接层通过 seq 守卫的事件注入(全窗由连接层交付)。 */
  seed(events: EventEnvelope[]): void
  append(evt: EventEnvelope): void
  updateFacts(patch: Partial<SessionFacts>): void
  snapshot(): ConversationSnapshot | null
  /** 裸源(host 面)。 */
  conversation: HostObservable<ConversationSnapshot | null>
  provideInfo: HostObservable<{
    sessionId: string | undefined
    hooks: Record<string, HostObservable<unknown> | undefined>
    props: Record<string, unknown>
  }>
  list: HostObservable<SessionListShape>
  setHasMore(v: boolean): void
  setLoadingOlder(v: boolean): void
  get loadingOlder(): boolean
  setLoadOlder(fn: (() => Promise<boolean>) | null): void
  loadOlder(sessionId: string): Promise<boolean>
  lastSeq(): number | null
  eventCount(): number
  /** 注册表就绪时装配(installDsh 调用;幂等)。 */
  attach(eventsRegistry: ConversationEventDefinitions, viewsRegistry: ConversationViewDefinitions): void
}

/** 组装会话源:适配器每次全窗折叠(O(n),窗 = history 200/页 + live)。
 *
 * 注册表(events/views)由 installDsh 在装配时提供 → attach();attach 前
 * rebuild() 为空转(实例在 socket select 前创建,装配时机在面板 render)。
 */
export function createDshSession(): DshSessionStore {
  const adapter = new GhToDshEvents()
  let assembler: ConversationNodeAssembler | null = null

  let folded = new Map<number, EventEnvelope>()
  let facts: SessionFacts | null = null
  let hasMoreValue = false
  let loadingOlderValue = false
  let loadOlderFn: (() => Promise<boolean>) | null = null

  const conversation = createObservable<ConversationSnapshot | null>(null)
  // 惰性输入面(无 composer):ConversationSession 等标准钩子读 useInput——
  // 提供静态源以稳定回归(字段按会话方消费面最低形状)。
  const input = createObservable<Record<string, unknown>>({
    draft: '',
    images: [],
    blocked: false,
    composing: false,
    dirty: false,
    isBlank: false,
  })
  const provideInfo = createObservable<{
    sessionId: string | undefined
    hooks: Record<string, HostObservable<unknown> | undefined>
    props: Record<string, unknown>
  }>({ sessionId: undefined, hooks: {}, props: {} })
  const list = createObservable<SessionListShape>({ byId: {}, phase: 'empty' })

  function lastSeqValue(): number | null {
    let last: number | null = null
    for (const seq of folded.keys()) {
      last = last === null ? seq : Math.max(last, seq)
    }
    return last
  }

  function snapshot(): ConversationSnapshot | null {
    if (facts === null || assembler === null) return null
    const chat = assembler.get('chat') ?? EMPTY_CHAT_SNAPSHOT
    return {
      sessionId: facts.sessionId as never,
      views: assembler,
      chat,
      nodes: chat.legacy.nodes,
      turnTimings: chat.legacy.turnTimings,
      turnEnds: chat.legacy.turnEnds,
      partial: chat.legacy.partial,
      runningCalls: chat.legacy.runningCalls,
      running: facts.state === 'running',
      subagent: null,
      composerPhase: 'active',
      removed: false,
      openState: folded.size === 0 ? 'loading' : 'open',
      openError: null,
      blank: false,
      hasMore: hasMoreValue,
      loadingOlder: loadingOlderValue,
      queue: [],
      pending: [],
      promptError: null,
      lastAgentError: null,
    }
  }

  function rebuild(): void {
    if (assembler === null) return
    adapter.reset()
    const entries: ConversationEventInput[] = []
    for (const evt of [...folded.values()].sort((a, b) => a.seq - b.seq)) {
      for (const event of adapter.translate(evt)) entries.push({ event, view: undefined })
    }
    try {
      assembler.replaceWindow(entries, hasMoreValue)
      assembler.flush() // flush() 才是视图物化入口(dsh Session 的发布节奏);replace 仅登记
    } catch (error) {
      console.error('[gh-puller/dsh] assemble 失败(事件窗不稳,等待下批):', error)
    }
    conversation.set(snapshot())
  }

  const store: DshSessionStore = {
    reset(next) {
      facts = next
      folded = new Map()
      hasMoreValue = false
      loadingOlderValue = false
      adapter.reset()
      provideInfo.set(next === null
        ? { sessionId: undefined, hooks: {}, props: {} }
        : { sessionId: next.sessionId, hooks: { session: conversation, input }, props: {} })
      list.set(next === null
        ? { byId: {}, phase: 'empty' }
        : {
          byId: { [next.sessionId]: {
            id: next.sessionId,
            displayTitle: next.label || next.sessionId,
            cwd: '',
            state: next.state,
            run_id: next.runId ?? null,
            provider: next.provider ?? '',
            model: next.model ?? '',
          } },
          phase: 'ready',
        })
      rebuild()
    },
    seed(events) {
      for (const evt of events) folded.set(evt.seq, evt)
      rebuild()
    },
    append(evt) {
      folded.set(evt.seq, evt)
      if (evt.type === 'session/start' && facts !== null) {
        const data = evt.data as Record<string, unknown>
        store.updateFacts({
          label: (evt as EventEnvelope & { label?: string }).label
            ?? (typeof data.label === 'string' ? data.label : undefined)
            ?? facts.label,
          runId: (evt as EventEnvelope & { run_id?: string | null }).run_id
            ?? (typeof data.run_id === 'string' ? data.run_id : null)
            ?? facts.runId,
        })
      }
      if (evt.type === 'session/end' && facts !== null) {
        const state = (evt.data as { state?: string }).state ?? 'completed'
        store.updateFacts({ state: state === 'aborted' ? 'aborted' : 'completed' })
      }
      rebuild()
    },
    updateFacts(patch) {
      if (facts === null) return
      facts = { ...facts, ...patch }
      rebuild()
    },
    snapshot: () => conversation.getSnapshot(),
    conversation,
    provideInfo,
    list,
    setHasMore(v) {
      if (hasMoreValue === v) return
      hasMoreValue = v
      rebuild()
    },
    setLoadingOlder(v) {
      loadingOlderValue = v
      conversation.set(snapshot())
    },
    get loadingOlder() {
      return loadingOlderValue
    },
    setLoadOlder(fn) { loadOlderFn = fn },
    async loadOlder(_sessionId) {
      if (loadOlderFn === null) return false
      return loadOlderFn()
    },
    lastSeq: lastSeqValue,
    eventCount: () => folded.size,
    attach(eventsRegistry, viewsRegistry) {
      assembler = new ConversationNodeAssembler(eventsRegistry, viewsRegistry)
      rebuild()
    },
  }
  return store
}
