/** Adapt canonical monitor events to the existing DSH presentation components. */

import type { EventEnvelope, Message } from '../../../monitor-data/types'
import type { CallId, MessageId } from '../llm/brand.ts'
import type { AssistantMessage, ToolResultMessage, UserMessage } from '../llm/message.ts'
import type { ContentBlock, ToolResultBlock } from '../llm/types.ts'
import type { SessionEvent } from '../session/types.ts'

export const BLOCK_START_SEQ_OFFSET = -0.25

type DshEvent = SessionEvent
type SurfaceOp = 'append' | { op: 'replace'; start: number; end: number }

function messageId(evt: EventEnvelope, suffix = ''): MessageId {
  return `ghp:msg:${evt.session}:${evt.seq}${suffix}` as MessageId
}

function json(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value ?? {})
}

function text(value: unknown): string {
  if (typeof value === 'string') return value
  return JSON.stringify(value ?? '')
}

function blocksOf(message: Message): ContentBlock[] {
  const blocks: ContentBlock[] = []
  for (const block of message.content) {
    if (block.type === 'text' && typeof block.text === 'string') {
      blocks.push({ type: 'text', text: block.text })
    } else if (block.type === 'reasoning' && typeof block.text === 'string') {
      blocks.push({ type: 'reasoning', text: block.text })
    } else if (block.type === 'tool_call') {
      blocks.push({
        type: 'tool-call',
        id: String(block.callId ?? '') as CallId,
        name: String(block.name ?? ''),
        arguments: json(block.arguments),
      })
    } else if (typeof block.text === 'string') {
      blocks.push({ type: 'text', text: block.text })
    }
  }
  return blocks
}

function userMessage(evt: EventEnvelope, message: Message, suffix = ''): UserMessage {
  return {
    id: messageId(evt, suffix),
    role: 'user',
    content: blocksOf(message),
    source: message.role === 'user'
      ? { kind: 'user' }
      : { kind: 'plugin', plugin: 'gh-puller-context', form: message.role },
  } as UserMessage
}

function assistantMessage(evt: EventEnvelope, message: Message, suffix = ''): AssistantMessage {
  return {
    id: messageId(evt, suffix),
    role: 'assistant',
    content: blocksOf(message),
    source: { kind: 'model', provider: 'provider', model: '', callId: '', purpose: 'generate' },
  } as AssistantMessage
}

/** The only DSH-facing state: marker counters and its synthetic visible sequence. */
export class GhToDshEvents {
  private turn = 0
  private step = 0
  private model: Record<string, unknown> = {}
  private surface: number[] = []
  private readonly starts = new Set<string>()

  reset(): void {
    this.turn = 0
    this.step = 0
    this.model = {}
    this.surface = []
    this.starts.clear()
  }

  private make(evt: EventEnvelope, type: DshEvent['type'], data: Record<string, unknown>,
    seq = evt.seq, surfaceOp?: SurfaceOp): DshEvent {
    return {
      type,
      seq,
      time: Math.round(evt.ts * 1000),
      ...(surfaceOp === undefined ? {} : { surfaceOp }),
      data,
    } as DshEvent
  }

  private addSurface(seq: number, op: SurfaceOp): void {
    if (op === 'append') {
      this.surface.push(seq)
      return
    }
    const start = this.surface.indexOf(op.start)
    const end = this.surface.indexOf(op.end)
    if (start >= 0 && end >= start) this.surface.splice(start, end - start + 1, seq)
  }

  private contextMessage(evt: EventEnvelope, message: Message, seq: number,
    op: SurfaceOp, suffix = '', sources?: number[]): DshEvent {
    this.addSurface(seq, op)
    const event = message.role === 'assistant'
      ? this.make(evt, 'assistant/message', {
        turn: this.turn || 1,
        step: this.step || 1,
        message: assistantMessage(evt, message, suffix),
      }, seq, op)
      : this.make(evt, 'user/message', {
        ...userMessage(evt, message, suffix),
      } as Record<string, unknown>, seq, op)
    if (sources !== undefined) {
      (event as DshEvent & { sourceEventSeqs: number[] }).sourceEventSeqs = sources
    }
    return event
  }

  private appendMessage(evt: EventEnvelope): DshEvent[] {
    if (evt.type === 'context/append/tool') return []
    const message = { ...evt.data } as Message
    if (evt.type !== 'context/append') message.role = evt.type.slice('context/append/'.length)
    return [this.contextMessage(evt, message, evt.seq, 'append')]
  }

  private setContext(evt: EventEnvelope): DshEvent[] {
    const messages = (evt.data.messages ?? []) as Message[]
    if (messages.length === 0) {
      if (this.surface.length === 0) return []
      const op: SurfaceOp = {
        op: 'replace', start: this.surface[0], end: this.surface.at(-1)!,
      }
      return [this.contextMessage(
        evt, { role: 'assistant', content: [] }, evt.seq, op, ':empty', [...this.surface])]
    }
    const previous = [...this.surface]
    return messages.map((message, index) => {
      const seq = evt.seq + index / (messages.length + 1)
      const op: SurfaceOp = index === 0 && previous.length > 0
        ? { op: 'replace', start: previous[0], end: previous.at(-1)! }
        : 'append'
      return this.contextMessage(
        evt, message, seq, op, `:${index}`, index === 0 ? previous : undefined)
    })
  }

  private delta(evt: EventEnvelope): DshEvent[] {
    const data = evt.data
    const index = Number(data.index ?? 0)
    const requestId = String(data.requestId)
    const blockType = evt.type === 'model/delta/reasoning'
      ? 'reasoning'
      : evt.type === 'model/delta/tool-call' ? 'tool-call' : 'text'
    const key = `${requestId}:${blockType}:${index}`
    const out: DshEvent[] = []
    if (!this.starts.has(key)) {
      this.starts.add(key)
      out.push(this.make(evt, 'assistant/chunk', {
        turn: this.turn || 1,
        step: this.step || 1,
        chunk: { type: 'block-start', index, blockType },
      }, evt.seq + BLOCK_START_SEQ_OFFSET))
    }
    const chunk = blockType === 'tool-call'
      ? {
          type: 'tool-call-delta',
          index,
          id: String(data.callId ?? '') as CallId,
          name: String(data.name ?? ''),
          argumentsDelta: String(data.argumentsDelta ?? ''),
        }
      : {
          type: blockType === 'reasoning' ? 'reasoning-delta' : 'text-delta',
          index,
          text: String(data.text ?? ''),
        }
    out.push(this.make(evt, 'assistant/chunk', {
      turn: this.turn || 1,
      step: this.step || 1,
      chunk,
    }))
    return out
  }

  private toolEnd(evt: EventEnvelope): DshEvent[] {
    const callId = String(evt.data.callId) as CallId
    const failed = evt.data.error !== undefined
    const block: ToolResultBlock = {
      type: 'tool-result',
      toolCallId: callId,
      content: [{ type: 'text', text: text(failed ? evt.data.error : evt.data.result) }],
      isError: failed,
    }
    const message: ToolResultMessage = {
      id: messageId(evt),
      role: 'user',
      content: [block],
      source: { kind: 'tool', callId },
    }
    this.addSurface(evt.seq, 'append')
    return [this.make(evt, 'tool/result', {
      turn: this.turn || 1,
      step: this.step || 1,
      message,
    }, evt.seq, 'append')]
  }

  translate(evt: EventEnvelope): DshEvent[] {
    if (evt.type.startsWith('context/append')) return this.appendMessage(evt)
    if (evt.type.startsWith('model/delta/')) return this.delta(evt)
    switch (evt.type) {
      case 'turn/start':
        this.turn += 1
        this.step = 0
        return [this.make(evt, 'turn/start', { turn: this.turn })]
      case 'turn/end':
        return [this.make(evt, 'turn/end', {
          turn: this.turn || 1,
          reason: evt.data.outcome === 'completed'
            ? { kind: 'completed' }
            : { kind: 'aborted', reason: { kind: 'canceled' } },
        })]
      case 'step/start':
        this.step += 1
        return [this.make(evt, 'step/start', { turn: this.turn || 1, step: this.step })]
      case 'step/end':
        return [this.make(evt, 'step/end', { turn: this.turn || 1, step: this.step || 1 })]
      case 'model/set':
        this.model = evt.data
        return []
      case 'header/set':
        return [this.make(evt, 'request/header', {
          header: {
            config: this.model,
            system: ((evt.data.instructions ?? []) as Array<Record<string, unknown>>)
              .map(block => String(block.text ?? '')).filter(Boolean).join('\n'),
            tools: evt.data.tools ?? [],
          },
          reason: 'change',
        })]
      case 'context/set':
        return this.setContext(evt)
      case 'tool/start':
        return [this.make(evt, 'tool/call', {
          turn: this.turn || 1,
          step: this.step || 1,
          callId: String(evt.data.callId) as CallId,
          name: String(evt.data.name ?? ''),
          arguments: json(evt.data.arguments),
        })]
      case 'tool/end':
        return this.toolEnd(evt)
      default:
        return []
    }
  }
}
