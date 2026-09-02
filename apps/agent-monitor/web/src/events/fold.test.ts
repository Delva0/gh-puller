import { describe, expect, it } from 'vitest'
import { RunFold } from './fold'
import { foldState, stateAt } from './state'
import type { EventEnvelope } from './types'

function evt(type: string, seq: number, data: Record<string, unknown> = {}): EventEnvelope {
  return { seq, ts: 1, session: 's1', type, data }
}

function message(role: string, value: string) {
  return {
    type: 'message',
    role,
    content: [{ type: role === 'assistant' ? 'output_text' : 'input_text', text: value }],
  }
}

function append(role: string, seq: number, value: string): EventEnvelope {
  return evt(`context/append/${role}`, seq, { items: [message(role, value)] })
}

describe('canonical state fold', () => {
  it('restores Agent and Context at any event prefix', () => {
    const events = [
      evt('agent/set', 0, { agent: 'custom', config: { mode: 'default', cwd: '/x' } }),
      evt('context/set', 1, { items: [{
        type: 'message',
        role: 'system',
        content: [
          { type: 'instruction', text: 'system' },
          { type: 'tool_defs', tools: [{ name: 'read', inputSchema: {} }] },
        ],
      }] }),
      append('user', 2, 'q'),
      evt('model/request', 3, { requestId: 'r1', model: 'm1', provider: 'p' }),
      append('assistant', 4, 'a'),
      evt('agent/set/mode', 5, { mode: 'plan' }),
      evt('model/request', 6, { requestId: 'r2', model: 'm2' }),
    ]
    expect(stateAt(events, 3).context.map(item => item.role))
      .toEqual(['system', 'user'])
    const state = foldState(events)
    expect(state.agent).toEqual({ agent: 'custom', config: { mode: 'plan', cwd: '/x' } })
    expect(state.context[0]?.content?.[1]).toMatchObject({
      type: 'tool_defs',
      tools: [{ name: 'read' }],
    })
    expect(state.context.map(item => item.role)).toEqual(['system', 'user', 'assistant'])

    const request = new RunFold()
    request.applyBatch(events)
    const requests = request.modelActivity()
    expect(requests[0].stateAtRequest).toMatchObject({
      agent: { agent: 'custom', config: { mode: 'default' } },
      context: [{ role: 'system' }, { role: 'user' }],
    })
    expect(requests[0].request).toMatchObject({ model: 'm1', provider: 'p' })
    expect(requests[1].stateAtRequest.agent?.config.mode).toBe('plan')
  })

  it('context/set replaces the complete sequence and generic append keeps custom roles', () => {
    const state = foldState([
      append('user', 0, 'old'),
      evt('context/set', 1, {
        items: [message('assistant', 'summary')],
      }),
      evt('context/append', 2, {
        items: [message('critic', 'note')],
      }),
    ])
    expect(state.context.map(item => item.role)).toEqual(['assistant', 'critic'])
  })
})

describe('RunFold', () => {
  it('rejects a live gap without corrupting the complete prefix', () => {
    const fold = new RunFold()
    fold.applyBatch([append('user', 0, 'q')], 3)
    expect(fold.ingestBatch([
      evt('model/request', 3, { requestId: 'r1' }),
      evt('model/delta/text', 4, { requestId: 'r1', index: 0, text: '你' }),
      evt('model/delta/text', 6, { requestId: 'r1', index: 0, text: '好' }),
    ])).toBe('gap')
    expect(fold.modelActivity()).toEqual([])
    expect(fold.state().context.map(item => item.role)).toEqual(['user'])

    expect(fold.ingestBatch([
      evt('model/request', 3, { requestId: 'r1' }),
      evt('model/delta/text', 4, { requestId: 'r1', index: 0, text: '你' }),
      evt('model/delta/text', 5, { requestId: 'r1', index: 0, text: '好' }),
      evt('model/response', 6, {
        requestId: 'r1', output: [message('assistant', '你好')],
      }),
      append('assistant', 7, '你好'),
    ])).toBe('ok')
    expect(fold.state().context.map(item => item.role)).toEqual(['user', 'assistant'])
    expect(fold.modelActivity()[0].text).toBe('你好')
    expect(fold.modelActivity()[0].deltaCount).toBe(2)
  })

  it('keeps replayable state identical when stream deltas are compacted away', () => {
    const raw = [
      evt('agent/set', 0, { agent: 'custom', config: { model: 'configured' } }),
      evt('context/set', 1, { items: [] }),
      evt('turn/start', 2),
      evt('step/start', 3),
      append('user', 4, 'question'),
      evt('model/request', 5, { requestId: 'r1' }),
      evt('model/delta/text', 6, { requestId: 'r1', index: 0, text: 'answer' }),
      evt('model/response', 7, {
        requestId: 'r1', output: [message('assistant', 'answer')],
      }),
      append('assistant', 8, 'answer'),
      evt('step/end', 9),
      evt('turn/end', 10),
    ]
    const streamed = new RunFold()
    streamed.applyBatch(raw)
    const compact = new RunFold()
    compact.applyBatch(raw.filter(event => !event.type.startsWith('model/delta/')), 11)

    expect(compact.state()).toEqual(streamed.state())
    expect(compact.stepCount()).toBe(1)
    expect(streamed.modelActivity()[0].text).toBe('answer')
    expect(compact.modelActivity()[0].output).toEqual(streamed.modelActivity()[0].output)
  })

  it('correlates tool activity without changing context', () => {
    const fold = new RunFold()
    fold.applyBatch([
      append('assistant', 0, ''),
      evt('tool/start', 1, { callId: 'c1', name: 'read', arguments: { path: 'a.py' } }),
      evt('tool/end', 2, { callId: 'c1', result: 'body' }),
    ])
    expect(fold.toolActivity()[0]).toMatchObject({
      callId: 'c1', name: 'read', arguments: { path: 'a.py' }, result: 'body',
    })
    expect(fold.state().context).toHaveLength(1)
  })

  it('tracks interleaved model requests independently', () => {
    const fold = new RunFold()
    fold.applyBatch([
      evt('model/request', 0, { requestId: 'left', model: 'planner' }),
      evt('model/request', 1, { requestId: 'right', model: 'writer' }),
      evt('model/delta/text', 2, { requestId: 'right', index: 0, text: 'R' }),
      evt('model/delta/text', 3, { requestId: 'left', index: 0, text: 'L' }),
      evt('model/response', 4, {
        requestId: 'left', output: [message('assistant', 'L')],
      }),
    ])
    expect(fold.modelActivity().map(request => [request.requestId, request.text]))
      .toEqual([['left', 'L'], ['right', 'R']])
    expect(fold.activeModels().map(request => request.requestId)).toEqual(['right'])
  })
})
