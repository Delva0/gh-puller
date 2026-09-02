/** Feed canonical session windows into the existing DSH presentation assembler. */
import type { EventEnvelope } from '../../../monitor-data/types'
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

/** Session row consumed by breadcrumbs and status presentation. */
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
  /** Seed a sequence-guarded canonical event window. */
  seed(events: EventEnvelope[], hasMore?: boolean): void
  append(evt: EventEnvelope): void
  appendBatch(events: EventEnvelope[]): void
  updateFacts(patch: Partial<SessionFacts>): void
  snapshot(): ConversationSnapshot | null
  /** Observable surfaces consumed by the DSH host. */
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
  /** Attach presentation registries once they are installed. */
  attach(eventsRegistry: ConversationEventDefinitions, viewsRegistry: ConversationViewDefinitions): void
}

/** Build a presentation source from replayed history and incremental live events. */
export function createDshSession(): DshSessionStore {
  const adapter = new GhToDshEvents()
  let assembler: ConversationNodeAssembler | null = null
  let assembledLastSeq: number | null = null
  let rebuildPending = false

  let folded = new Map<number, EventEnvelope>()
  let facts: SessionFacts | null = null
  let hasMoreValue = false
  let loadingOlderValue = false
  let loadOlderFn: (() => Promise<boolean>) | null = null

  const conversation = createObservable<ConversationSnapshot | null>(null)
  // The read-only monitor provides the minimum static composer shape expected by DSH.
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

  function presentationWindow(): EventEnvelope[] {
    const ordered = [...folded.values()].sort((a, b) => a.seq - b.seq)
    const reset = ordered.findLastIndex(evt => evt.type === 'context/set')
    if (reset < 0) return ordered
    const prefix = ordered.slice(0, reset)
    const model = prefix.findLast(evt => evt.type === 'model/set')
    return [...[model].filter(evt => evt !== undefined), ...ordered.slice(reset)]
  }

  function rebuild(): void {
    if (assembler === null) return
    adapter.reset()
    const entries: ConversationEventInput[] = []
    for (const evt of presentationWindow()) {
      for (const event of adapter.translate(evt)) entries.push({ event, view: undefined })
    }
    try {
      assembler.replaceWindow(entries, hasMoreValue)
      assembler.flush()
      assembledLastSeq = entries.length === 0 ? null : Math.max(...entries.map(entry => entry.event.seq))
      rebuildPending = false
    } catch (error) {
      rebuildPending = true
      console.error('[gh-puller/dsh] deferred unstable event window:', error)
    }
    conversation.set(snapshot())
  }

  function publishFacts(): void {
    if (facts === null) return
    list.set({
      byId: { [facts.sessionId]: {
        id: facts.sessionId,
        displayTitle: facts.label || facts.sessionId,
        cwd: '',
        state: facts.state,
        run_id: facts.runId ?? null,
        provider: facts.provider ?? '',
        model: facts.model ?? '',
      } },
      phase: 'ready',
    })
  }

  function applyFacts(evt: EventEnvelope): void {
    if (facts === null) return
    if (evt.type === 'session/start') {
      const data = evt.data as Record<string, unknown>
      facts = {
        ...facts,
        label: (evt as EventEnvelope & { label?: string }).label
          ?? (typeof data.label === 'string' ? data.label : facts.label),
        runId: typeof data.runId === 'string' ? data.runId : facts.runId,
      }
      publishFacts()
    } else if (evt.type === 'model/set') {
      const data = evt.data as Record<string, unknown>
      facts = {
        ...facts,
        provider: typeof data.provider === 'string' ? data.provider : facts.provider,
        model: typeof data.model === 'string' ? data.model : facts.model,
      }
      publishFacts()
    } else if (evt.type === 'session/end') {
      const outcome = (evt.data as { outcome?: string }).outcome
      facts = { ...facts, state: outcome === 'completed' ? 'completed' : 'aborted' }
      publishFacts()
    }
  }

  const store: DshSessionStore = {
    reset(next) {
      facts = next
      folded = new Map()
      hasMoreValue = false
      loadingOlderValue = false
      adapter.reset()
      assembledLastSeq = null
      rebuildPending = false
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
    seed(events, hasMore) {
      for (const evt of events) folded.set(evt.seq, evt)
      if (hasMore !== undefined) hasMoreValue = hasMore
      rebuild()
    },
    append(evt) {
      store.appendBatch([evt])
    },
    appendBatch(events) {
      const fresh = [...events]
        .filter(evt => !folded.has(evt.seq))
        .sort((a, b) => a.seq - b.seq)
      if (fresh.length === 0) return
      const resetsContext = fresh.some(evt => evt.type === 'context/set')
      const entries: ConversationEventInput[] = []
      for (const evt of fresh) {
        folded.set(evt.seq, evt)
        applyFacts(evt)
        if (assembler !== null && !resetsContext) {
          for (const event of adapter.translate(evt)) entries.push({ event, view: undefined })
        }
      }
      if (assembler === null) return
      if (resetsContext) {
        rebuild()
        return
      }
      if (rebuildPending) {
        rebuild()
        return
      }
      let cursor = assembledLastSeq
      let monotonic = true
      for (const entry of entries) {
        if (cursor !== null && entry.event.seq <= cursor) monotonic = false
        cursor = entry.event.seq
      }
      if (!monotonic) {
        // Rebuild when presentation expansion cannot preserve strict sequence order.
        rebuild()
        return
      }
      cursor = assembledLastSeq
      for (const entry of entries) {
        assembler.append(entry)
        cursor = entry.event.seq
      }
      assembledLastSeq = cursor
      assembler.flush()
      conversation.set(snapshot())
    },
    updateFacts(patch) {
      if (facts === null) return
      facts = { ...facts, ...patch }
      publishFacts()
      conversation.set(snapshot())
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
