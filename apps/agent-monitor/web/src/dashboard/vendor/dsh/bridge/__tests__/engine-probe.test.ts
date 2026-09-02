import { describe, expect, it } from 'vitest'
import type { EventEnvelope } from '../../../../monitor-data/types'
import {
  ConversationEventRegistry,
  ConversationNodeAssembler,
  ConversationViewRegistry,
  type ConversationEventInput,
} from '../../runtime/src/client'
import { registerConversationNodes } from '../../ui-conversation/src/client/conversation-nodes/register'
import { GhToDshEvents } from '../dsh-events'

function evt(type: string, seq: number, data: Record<string, unknown> = {}): EventEnvelope {
  return { seq, ts: 1700000000.5, session: 'ns/u1', type, data }
}

function face(events: ConversationEventRegistry, views: ConversationViewRegistry) {
  return {
    conversationEvents: events,
    conversationViews: views,
    slots: { inject: () => () => {}, register: () => () => {} } as never,
    locale: { register: () => {}, bind: () => () => '' },
    hostDescription: { getSnapshot: () => undefined, subscribe: () => () => {} },
    loadOlder: async () => false,
  }
}

function assemble(raws: EventEnvelope[]) {
  const events = new ConversationEventRegistry()
  const views = new ConversationViewRegistry()
  registerConversationNodes(face(events, views))
  const adapter = new GhToDshEvents()
  const entries: ConversationEventInput[] = []
  for (const raw of raws) {
    for (const event of adapter.translate(raw)) entries.push({ event, view: undefined })
  }
  const assembler = new ConversationNodeAssembler(events, views)
  assembler.replaceWindow(entries, false)
  assembler.flush()
  return assembler
}

describe('DSH presentation engine', () => {
  it('assembles canonical context and marker events into chat nodes', () => {
    const chat = assemble([
      evt('turn/start', 0),
      evt('step/start', 1),
      evt('context/append/user', 2, { content: [{ type: 'text', text: 'probe' }] }),
      evt('model/request', 3, { requestId: 'r1' }),
      evt('model/delta/text', 4, { requestId: 'r1', index: 0, text: 'hello' }),
      evt('model/response', 5, {
        requestId: 'r1',
        message: { role: 'assistant', content: [{ type: 'text', text: 'hello' }] },
      }),
      evt('context/append/assistant', 6, { content: [{ type: 'text', text: 'hello' }] }),
      evt('step/end', 7),
      evt('turn/end', 8, { outcome: 'completed' }),
    ]).get('chat')
    expect((chat?.order.length ?? 0)).toBeGreaterThan(0)
    expect((chat?.order ?? []).map(key => chat!.nodes.get(key)?.kind))
      .toEqual(['user', 'assistant-step', 'turn-tail'])
  })

  it('keeps assistant tool calls before their local results', () => {
    const chat = assemble([
      evt('turn/start', 0),
      evt('step/start', 1),
      evt('context/append/user', 2, { content: [{ type: 'text', text: 'run' }] }),
      evt('context/append/assistant', 3, {
        content: [{ type: 'tool_call', callId: 'c1', name: 'Bash', arguments: { command: 'ls' } }],
      }),
      evt('tool/start', 4, { callId: 'c1', name: 'Bash', arguments: { command: 'ls' } }),
      evt('tool/end', 5, { callId: 'c1', result: 'ok' }),
      evt('context/append/tool', 6, {
        callId: 'c1', content: [{ type: 'text', text: 'ok' }],
      }),
      evt('step/end', 7),
      evt('turn/end', 8, { outcome: 'completed' }),
    ]).get('chat')
    const kinds = (chat?.order ?? []).map(key => chat!.nodes.get(key)?.kind)
    expect(kinds).toEqual(['user', 'tool-call', 'turn-tail'])
  })

})
