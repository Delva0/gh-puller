import { describe, expect, it } from 'vitest'
import type { EventEnvelope } from '../../../../monitor-data/types'
import { BLOCK_START_SEQ_OFFSET, GhToDshEvents } from '../dsh-events'

function evt(type: string, seq: number, data: Record<string, unknown> = {}): EventEnvelope {
  return { seq, ts: 1700000000.5, session: 'ns/u1', type, data }
}

describe('canonical to DSH presentation adapter', () => {
  it('maps canonical context messages without protocol metadata', () => {
    const adapter = new GhToDshEvents()
    adapter.translate(evt('turn/start', 0))
    const [user] = adapter.translate(evt('context/append/user', 1, {
      content: [{ type: 'text', text: 'hi' }],
    }))
    const [assistant] = adapter.translate(evt('context/append/assistant', 2, {
      content: [{ type: 'text', text: 'hello' }],
    }))
    expect(user).toMatchObject({ type: 'user/message', seq: 1, surfaceOp: 'append' })
    expect(assistant).toMatchObject({ type: 'assistant/message', seq: 2, surfaceOp: 'append' })
    expect(user.time).toBe(1700000000500)
  })

  it('maps correlated model deltas to the DSH block language', () => {
    const adapter = new GhToDshEvents()
    const first = adapter.translate(evt('model/delta/text', 5, {
      requestId: 'r1', index: 0, text: '世',
    }))
    expect(first).toHaveLength(2)
    expect(first[0]).toMatchObject({
      type: 'assistant/chunk',
      seq: 5 + BLOCK_START_SEQ_OFFSET,
      data: { chunk: { type: 'block-start', blockType: 'text' } },
    })
    expect(first[1]).toMatchObject({ data: { chunk: { type: 'text-delta', text: '世' } } })
    expect(adapter.translate(evt('model/delta/text', 6, {
      requestId: 'r1', index: 0, text: '界',
    }))).toHaveLength(1)
  })

  it('maps tool activity while the tool context commit stays presentation-neutral', () => {
    const adapter = new GhToDshEvents()
    const [start] = adapter.translate(evt('tool/start', 1, {
      callId: 'c1', name: 'Bash', arguments: { command: 'ls' },
    }))
    const [end] = adapter.translate(evt('tool/end', 2, { callId: 'c1', result: 'ok' }))
    expect(start).toMatchObject({ type: 'tool/call', data: { arguments: '{"command":"ls"}' } })
    expect(end).toMatchObject({ type: 'tool/result', surfaceOp: 'append' })
    expect(adapter.translate(evt('context/append/tool', 3, {
      callId: 'c1', content: [{ type: 'text', text: 'ok' }],
    }))).toEqual([])
  })

  it('expresses context/set as one replacement followed by appends', () => {
    const adapter = new GhToDshEvents()
    adapter.translate(evt('context/append/user', 1, {
      content: [{ type: 'text', text: 'old' }],
    }))
    const out = adapter.translate(evt('context/set', 5, {
      messages: [
        { role: 'user', content: [{ type: 'text', text: 'summary' }] },
        { role: 'assistant', content: [{ type: 'text', text: 'ready' }] },
      ],
    }))
    expect(out).toHaveLength(2)
    expect((out[0] as { surfaceOp?: unknown }).surfaceOp)
      .toEqual({ op: 'replace', start: 1, end: 1 })
    expect((out[1] as { surfaceOp?: unknown }).surfaceOp).toBe('append')
  })

  it('derives DSH request inspection from system context', () => {
    const adapter = new GhToDshEvents()
    expect(adapter.translate(evt('model/set', 0, {
      model: 'm', provider: 'p', parameters: { temperature: 0 },
    }))).toEqual([])
    const out = adapter.translate(evt('context/set', 1, { messages: [
      { role: 'system', content: [
        { type: 'text', text: 'system' },
        { type: 'tool_definition', name: 'read', description: 'Read', inputSchema: {
          type: 'object',
        } },
      ] },
      { role: 'user', content: [{ type: 'text', text: 'q' }] },
    ] }))
    expect(out).toHaveLength(1)
    const [header] = adapter.translate(evt('model/request', 2, { requestId: 'r1' }))
    expect(header).toMatchObject({
      type: 'request/header',
      data: { header: {
        config: { provider: 'p', model: 'm', temperature: 0 },
        system: 'system',
        tools: [{ name: 'read', description: 'Read', parameters: { type: 'object' } }],
      } },
    })
    expect(out[0]).toMatchObject({ type: 'user/message' })
  })
})
