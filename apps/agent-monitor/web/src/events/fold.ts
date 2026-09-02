/** Incrementally fold a session while keeping canonical state and activity separate. */

import {
  applyStateEvent,
  emptyState,
  foldState,
} from './state'
import type { CanonicalState, EventEnvelope, Item, ModelActivity, ToolActivity } from './types'

export type IngestResult = 'ok' | 'dup' | 'gap'

export class RunFold {
  private events: EventEnvelope[] = []
  private nextSeq = 0

  applyBatch(batch: EventEnvelope[], cursor?: number): void {
    const bySeq = new Map<number, EventEnvelope>()
    for (const evt of this.events) bySeq.set(evt.seq, evt)
    for (const evt of batch) bySeq.set(evt.seq, evt)
    this.events = [...bySeq.values()].sort((a, b) => a.seq - b.seq)
    const eventCursor = this.events.length === 0 ? 0 : this.events.at(-1)!.seq + 1
    this.nextSeq = Math.max(eventCursor, cursor ?? 0)
  }

  ingestBatch(batch: EventEnvelope[]): IngestResult {
    let cursor = this.nextSeq
    const fresh: EventEnvelope[] = []
    for (const evt of [...batch].sort((a, b) => a.seq - b.seq)) {
      if (evt.seq < cursor) continue
      if (evt.seq > cursor) return 'gap'
      fresh.push(evt)
      cursor += 1
    }
    if (fresh.length === 0) return 'dup'
    this.events = [...this.events, ...fresh]
    this.nextSeq = cursor
    return 'ok'
  }

  state(): CanonicalState {
    return foldState(this.events)
  }

  modelActivity(): ModelActivity[] {
    const requests = new Map<string, ModelActivity>()
    const state = emptyState()
    for (const evt of this.events) {
      if (applyStateEvent(state, evt)) continue
      const requestId = typeof evt.data.requestId === 'string' ? evt.data.requestId : null
      if (evt.type === 'model/request' && requestId !== null) {
        requests.set(requestId, {
          requestId,
          requestSeq: evt.seq,
          request: { ...evt.data },
          stateAtRequest: {
            agent: state.agent === null
              ? null
              : { ...state.agent, config: { ...state.agent.config } },
            context: [...state.context],
          },
          text: '',
          reasoning: '',
          deltaCount: 0,
          toolCalls: new Map(),
        })
      } else if (evt.type.startsWith('model/delta/') && requestId !== null) {
        const request = requests.get(requestId)
        if (request === undefined) continue
        request.deltaCount += 1
        if (evt.type === 'model/delta/text') request.text += String(evt.data.text ?? '')
        if (evt.type === 'model/delta/reasoning') request.reasoning += String(evt.data.text ?? '')
        if (evt.type === 'model/delta/tool-call') {
          const index = Number(evt.data.index ?? 0)
          const previous = request.toolCalls.get(index)
          request.toolCalls.set(index, {
            callId: String(evt.data.callId ?? previous?.callId ?? ''),
            ...(typeof evt.data.name === 'string'
              ? { name: evt.data.name }
              : previous?.name ? { name: previous.name } : {}),
            arguments: `${previous?.arguments ?? ''}${String(evt.data.argumentsDelta ?? '')}`,
          })
        }
      } else if (evt.type === 'model/response' && requestId !== null) {
        const request = requests.get(requestId)
        if (request === undefined) continue
        request.responseSeq = evt.seq
        request.output = evt.data.output as Item[]
        request.usage = evt.data.usage as ModelActivity['usage']
        if (typeof evt.data.stopReason === 'string') request.stopReason = evt.data.stopReason
      }
    }
    return [...requests.values()].sort((a, b) => a.requestSeq - b.requestSeq)
  }

  toolActivity(): ToolActivity[] {
    const tools = new Map<string, ToolActivity>()
    for (const evt of this.events) {
      const callId = typeof evt.data.callId === 'string' ? evt.data.callId : null
      if (evt.type === 'tool/start' && callId !== null) {
        tools.set(callId, {
          callId,
          startSeq: evt.seq,
          ...(typeof evt.data.name === 'string' ? { name: evt.data.name } : {}),
          arguments: evt.data.arguments,
        })
      } else if (evt.type === 'tool/end' && callId !== null) {
        const tool = tools.get(callId) ?? { callId, startSeq: evt.seq }
        tool.endSeq = evt.seq
        tool.result = evt.data.result
        tool.error = evt.data.error
        tools.set(callId, tool)
      }
    }
    return [...tools.values()].sort((a, b) => a.startSeq - b.startSeq)
  }

  activeModels(requests = this.modelActivity()): ModelActivity[] {
    return requests.filter(request => request.responseSeq === undefined)
  }

  stepCount(): number {
    return this.events.filter(evt => evt.type === 'step/start').length
  }

  all(): EventEnvelope[] {
    return this.events
  }
}
