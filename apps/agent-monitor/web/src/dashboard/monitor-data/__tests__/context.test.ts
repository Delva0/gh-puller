import { describe, expect, it } from 'vitest'
import { RunFold } from '../fold'
import { foldRequestState, requestStateAt } from '../context'
import type { EventEnvelope } from '../types'

function evt(type: string, seq: number, data: Record<string, unknown> = {}): EventEnvelope {
  return { seq, ts: 1, session: 's1', type, data }
}

function append(role: string, seq: number, value: string): EventEnvelope {
  return evt(`context/append/${role}`, seq, { content: [{ type: 'text', text: value }] })
}

describe('canonical request-state fold', () => {
  it('restores model and context at any event prefix', () => {
    const events = [
      evt('model/set', 0, { model: 'm1', provider: 'p', parameters: {} }),
      evt('context/set', 1, { messages: [{
        role: 'system',
        content: [
          { type: 'text', text: 'system' },
          { type: 'tool_definition', name: 'read', inputSchema: {} },
        ],
      }] }),
      append('user', 2, 'q'),
      evt('model/request', 3, { requestId: 'r1' }),
      append('assistant', 4, 'a'),
      evt('model/set', 5, { model: 'm2', parameters: { temperature: 0 } }),
    ]
    expect(requestStateAt(events, 3).context.map(message => message.role))
      .toEqual(['system', 'user'])
    const state = foldRequestState(events)
    expect(state.model).toMatchObject({ model: 'm2', parameters: { temperature: 0 } })
    expect(state.context[0].content[1]).toMatchObject({ type: 'tool_definition', name: 'read' })
    expect(state.context.map(message => message.role)).toEqual(['system', 'user', 'assistant'])
  })

  it('context/set replaces the complete sequence and generic append keeps custom roles', () => {
    const state = foldRequestState([
      append('user', 0, 'old'),
      evt('context/set', 1, {
        messages: [{ role: 'assistant', content: [{ type: 'text', text: 'summary' }] }],
      }),
      evt('context/append', 2, {
        role: 'critic', content: [{ type: 'text', text: 'note' }],
      }),
    ])
    expect(state.context.map(message => message.role)).toEqual(['assistant', 'critic'])
  })
})

describe('RunFold', () => {
  it('keeps state and correlated activity independent across gaps', () => {
    const fold = new RunFold()
    fold.applyBatch([append('user', 0, 'q')], 3)
    expect(fold.ingestBatch([
      evt('model/request', 3, { requestId: 'r1' }),
      evt('model/delta/text', 4, { requestId: 'r1', index: 0, text: '你' }),
      evt('model/delta/text', 6, { requestId: 'r1', index: 0, text: '好' }),
    ])).toBe('gap')
    expect(fold.partial).toBe('你好')
    expect(fold.messages().map(message => message.role)).toEqual(['user'])

    fold.ingestBatch([
      evt('model/response', 7, {
        requestId: 'r1', message: { role: 'assistant', content: [{ type: 'text', text: '你好' }] },
      }),
      append('assistant', 8, '你好'),
    ])
    expect(fold.partial).toBe('')
    expect(fold.messages().map(message => message.role)).toEqual(['user', 'assistant'])
    expect(fold.modelActivity()[0].text).toBe('你好')
  })
})
