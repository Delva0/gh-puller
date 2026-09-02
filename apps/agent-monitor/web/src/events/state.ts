/** Fold canonical Agent and Context state at any event prefix. */

import type { AgentState, CanonicalState, EventEnvelope, Item } from './types'

export const CONTEXT_APPEND_TYPES = new Set([
  'context/append',
  'context/append/system',
  'context/append/user',
  'context/append/assistant',
  'context/append/tool',
])

export function appendedItems(evt: EventEnvelope): Item[] | null {
  if (!CONTEXT_APPEND_TYPES.has(evt.type)) return null
  return [...((evt.data.items ?? []) as Item[])]
}

function agentFacet(type: string): string | null {
  if (!type.startsWith('agent/set/')) return null
  const facet = type.slice('agent/set/'.length)
  return facet !== '' && !facet.includes('/') ? facet : null
}

export function emptyState(): CanonicalState {
  return { agent: null, context: [] }
}

export function applyStateEvent(state: CanonicalState, evt: EventEnvelope): boolean {
  if (evt.type === 'agent/set') {
    state.agent = {
      agent: typeof evt.data.agent === 'string' ? evt.data.agent : null,
      config: { ...((evt.data.config ?? {}) as Record<string, unknown>) },
    }
    return true
  }
  const facet = agentFacet(evt.type)
  if (facet !== null) {
    const agent: AgentState = state.agent ?? { agent: null, config: {} }
    state.agent = {
      ...agent,
      config: { ...agent.config, [facet]: evt.data[facet] },
    }
    return true
  }
  if (evt.type === 'context/set') {
    state.context = [...((evt.data.items ?? []) as Item[])]
    return true
  }
  const items = appendedItems(evt)
  if (items === null) return false
  state.context = [...state.context, ...items]
  return true
}

export function foldState(events: EventEnvelope[]): CanonicalState {
  const state = emptyState()
  for (const evt of [...events].sort((a, b) => a.seq - b.seq)) applyStateEvent(state, evt)
  return state
}

export function stateAt(events: EventEnvelope[], seq: number): CanonicalState {
  return foldState(events.filter(evt => evt.seq < seq))
}
