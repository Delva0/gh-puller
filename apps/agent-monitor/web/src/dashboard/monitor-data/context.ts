/** Fold canonical state events into the exact model request state at any prefix. */

import type { EventEnvelope, Message, RequestState } from './types'

export const CONTEXT_APPEND_TYPES = new Set([
  'context/append',
  'context/append/user',
  'context/append/assistant',
  'context/append/tool',
])

export function appendedMessage(evt: EventEnvelope): Message | null {
  if (!CONTEXT_APPEND_TYPES.has(evt.type)) return null
  const data = { ...evt.data } as Message
  if (evt.type !== 'context/append') data.role = evt.type.slice('context/append/'.length)
  return data
}

export function emptyRequestState(): RequestState {
  return { model: null, context: [] }
}

export function applyStateEvent(state: RequestState, evt: EventEnvelope): boolean {
  if (evt.type === 'model/set') {
    state.model = {
      model: String(evt.data.model),
      ...(typeof evt.data.provider === 'string' ? { provider: evt.data.provider } : {}),
      parameters: (evt.data.parameters ?? {}) as Record<string, unknown>,
    }
    return true
  }
  if (evt.type === 'context/set') {
    state.context = [...((evt.data.messages ?? []) as Message[])]
    return true
  }
  const message = appendedMessage(evt)
  if (message === null) return false
  state.context = [...state.context, message]
  return true
}

export function foldRequestState(events: EventEnvelope[]): RequestState {
  const state = emptyRequestState()
  for (const evt of [...events].sort((a, b) => a.seq - b.seq)) applyStateEvent(state, evt)
  return state
}

export function requestStateAt(events: EventEnvelope[], seq: number): RequestState {
  return foldRequestState(events.filter(evt => evt.seq < seq))
}
