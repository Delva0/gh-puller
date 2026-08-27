/**
 * gh-puller 事件流 → dsh SessionEvent 适配器(桥接层唯一新写的数据代码)。
 *
 * gh 侧事件分类学(gh_puller/agent/events.py)与 dsh 事件词表本就同构
 * ("对齐 dsh 核心不变量");差异点与处理:
 * - `assistant/chunk`:gh 为 text/thinking/tool_input 增量,dsh 为 block-start /
 *   *-delta / block-end 语言 → 按 (turn,step,index) 状态机合成;
 * - `session/start` / `session/end`:dsh 无此词汇(log 内只有 turn 间隔),
 *   run_id/label 等信息由桥接 host 面承载 → 丢弃;
 * - `error`:dsh 的失败语义走 `turn/end.reason.kind='error'` → 丢弃
 *   (turn-tail 仍按 turn/end 渲染);
 * - `context/inject` / `context/modify`:dsh 无对应 log 事件 → 丢弃;
 * - `request/context`、`todo/write`、`request/header`:直传(词汇一致);
 * - surface 事件携带 gh 的 `surfaceOp` 与 dsh `SurfaceOp` 同形('append' |
 *   {op:'replace',start,end})→ 直传;gh 的时间戳为秒,dsh 为毫秒 → ×1000。
 */
import type { EventEnvelope, Message as GhMessage } from '../../../monitor-data/types'
import type { SessionEvent } from '../session/types.ts'
import type { UserMessage, AssistantMessage, ToolResultMessage } from '../llm/message.ts'
import type { ContentBlock, TokenUsage } from '../llm/types.ts'
import type { CallId, MessageId } from '../llm/brand.ts'

const BASE = 'ghp'

/** dsh 需要 id 归属的消息 id(gh 无);从事件 id 派生。 */
function msgId(seed: string): MessageId {
  return `${BASE}:msg:${seed}` as MessageId
}

/** `tool/call` id 缺失时合成的 chunk 关联 id。 */
function chunkCallId(turn: number, step: number, index: number): CallId {
  return `${BASE}:chunk:${turn}.${step}.${index}` as CallId
}

/** gh 内容块 → dsh ContentBlock(静态消息素材)。 */
function toBlocks(message: GhMessage): ContentBlock[] {
  const blocks: ContentBlock[] = []
  for (const block of message.content) {
    const raw = block as Record<string, unknown>
    const type = raw.type
    if (type === 'text' && typeof raw.text === 'string') {
      blocks.push({ type: 'text', text: raw.text })
    } else if (type === 'thinking' && typeof raw.thinking === 'string') {
      blocks.push({ type: 'reasoning', text: raw.thinking })
    } else if (type === 'tool_use') {
      blocks.push({
        type: 'tool-call',
        id: (typeof raw.id === 'string' ? raw.id : '') as CallId,
        name: typeof raw.name === 'string' ? raw.name : '',
        arguments: JSON.stringify(raw.input ?? {}),
      })
    } else if (type === 'tool_result') {
      blocks.push({
        type: 'tool-result',
        toolCallId: (typeof raw.tool_use_id === 'string' ? raw.tool_use_id : '') as CallId,
        content: [{ type: 'text', text: typeof raw.content === 'string' ? raw.content : '' }],
        isError: raw.is_error === true,
      })
    } else if (typeof raw.text === 'string' && raw.text !== '') {
      blocks.push({ type: 'text', text: raw.text })
    }
  }
  return blocks
}

function toMessage(gh: GhMessage, eventId: string): UserMessage | AssistantMessage {
  const base = {
    id: msgId(eventId),
    role: gh.role,
    content: toBlocks(gh),
  }
  if (gh.role === 'user') {
    return { ...base, source: { kind: 'user' } } as UserMessage
  }
  return {
    ...base,
    source: { kind: 'model', provider: 'provider', model: '', callId: '', purpose: 'generate' },
  } as AssistantMessage
}

/** gh token usage → dsh TokenUsage(字段近似)。 */
function usage(input: unknown): TokenUsage | undefined {
  if (input == null || typeof input !== 'object') return undefined
  const u = input as Record<string, unknown>
  const details = (u.prompt_tokens_details ?? {}) as Record<string, unknown>
  const num = (v: unknown): number => (typeof v === 'number' ? v : 0)
  return {
    inputTokens: num(u.input_tokens ?? u.input ?? u.prompt_tokens),
    outputTokens: num(u.output_tokens ?? u.output ?? u.completion_tokens),
    cacheReadTokens: num(u.cache_read_tokens ?? u.cache_read ?? details.cached_tokens),
    cacheWriteTokens: num(u.cache_write_tokens ?? u.cache_write),
    reasoningTokens: num(u.reasoning),
  }
}

/** dsh 表面事件集合(isAppend/isReplacementSurfaceEvent 走顶层 surfaceOp)。 */
const SURFACE_TYPES: ReadonlySet<string> = new Set(['user/message', 'assistant/message', 'tool/result'])

type DshEvent = SessionEvent

/** 合成流式 chunk 的状态机:同一 (turn,step,index) 只发一次 block-start。 */
export class GhToDshEvents {
  private readonly starts = new Map<string, boolean>()

  reset(): void {
    this.starts.clear()
  }

  /** 翻译单个 gh 信封;返回 0..n 个 dsh 事件(多数 1)。 */
  translate(evt: EventEnvelope): DshEvent[] {
    const data = evt.data as Record<string, unknown>
    const num = (v: unknown): number | undefined => (typeof v === 'number' ? v : undefined)
    const turn = num(data.turn) ?? 1
    const step = num(data.step) ?? 1
    const mk = (type: DshEvent['type'], payload: Record<string, unknown>): DshEvent => {
      const event: Record<string, unknown> = {
        type: type as DshEvent['type'],
        seq: evt.seq,
        time: Math.round(evt.ts * 1000),
      }
      // surface 语义:dsh 的 surfaceOp 是事件顶层字段(data 内除外);
      // gh 信封放在 data.surfaceOp → 提升。
      if (SURFACE_TYPES.has(type)) {
        if (typeof payload.surfaceOp === 'string' || (payload.surfaceOp as { op?: string } | null)?.op) {
          event.surfaceOp = payload.surfaceOp
        }
        if (typeof payload.surfaceOp === 'string') delete payload.surfaceOp
        if (typeof (payload.surfaceOp as { op?: string } | null)?.op === 'string') delete payload.surfaceOp
      }
      event.data = payload
      event.ignorable = evt.ignorable === true ? true : undefined
      return event as DshEvent
    }
    const out: DshEvent[] = []

    switch (evt.type) {
      case 'turn/start':
        out.push(mk('turn/start', { turn })); break
      case 'turn/end': {
        const reasonKind = data.reason === 'aborted'
          ? { kind: 'aborted', reason: { kind: 'canceled' as never } }
          : { kind: 'completed' as const }
        out.push(mk('turn/end', { turn, reason: reasonKind })); break
      }
      case 'step/start':
        out.push(mk('step/start', { turn, step })); break
      case 'step/end':
        out.push(mk('step/end', { turn, step })); break
      case 'user/message': {
        // dsh 契约:user/message 的 data 即扁平 UserMessage(content/source 顶层),
        // 而非 gh 的 {message,source} 信封 → 展开。
        const ghMsg = toMessage(data.message as GhMessage, evt.id)
        const source = (data.source as { kind?: string; form?: string } | undefined)?.kind === 'context'
          ? { kind: 'plugin', plugin: 'gh-puller-context', form: (data.source as { form?: string }).form }
          : { kind: 'user' }
        out.push(mk('user/message', {
          id: ghMsg.id,
          role: 'user',
          content: ghMsg.content,
          source,
          surfaceOp: data.surfaceOp,
        } as Record<string, unknown>)); break
      }
      case 'assistant/message':
        out.push(mk('assistant/message', {
          turn,
          step,
          message: toMessage(data.message as GhMessage, evt.id) as AssistantMessage,
          usage: usage(data.usage),
          interrupted: data.interrupted === true ? true : undefined,
          surfaceOp: data.surfaceOp,
        } as Record<string, unknown>)); break
      case 'assistant/chunk': {
        const chunk = data.chunk as { type?: string; index?: number; text?: string; partial_json?: string }
        const index = num(chunk.index) ?? 0
        const key = `${turn}:${step}:${index}`
        const blockType: 'text' | 'reasoning' | 'tool-call' = chunk.type === 'thinking' ? 'reasoning'
          : chunk.type === 'tool_input' ? 'tool-call' : 'text'
        if (!this.starts.has(key)) {
          this.starts.set(key, true)
          out.push(mk('assistant/chunk', { turn, step, chunk: { type: 'block-start', index, blockType } }))
        }
        if (blockType === 'tool-call') {
          out.push(mk('assistant/chunk', {
            turn, step,
            chunk: {
              type: 'tool-call-delta', index,
              id: chunkCallId(turn, step, index),
              name: '',
              argumentsDelta: chunk.partial_json ?? '',
            },
          }))
        } else {
          out.push(mk('assistant/chunk', {
            turn, step,
            chunk: { type: blockType === 'text' ? 'text-delta' : 'reasoning-delta', index, text: chunk.text ?? '' },
          }))
        }
        break
      }
      case 'tool/call':
        out.push(mk('tool/call', {
          turn, step,
          callId: (typeof data.callId === 'string' ? data.callId : '') as CallId,
          name: typeof data.name === 'string' ? data.name : '',
          arguments: typeof data.arguments === 'string' ? data.arguments : '{}',
        })); break
      case 'tool/result': {
        const ghMsg = data.message as GhMessage | null | undefined
        const content: ContentBlock[] = ghMsg == null
          ? [{ type: 'text', text: '' }]
          : toBlocks(ghMsg)
        const message: ToolResultMessage = {
          id: msgId(evt.id),
          role: 'user',
          content: [{
            type: 'tool-result',
            toolCallId: (typeof data.callId === 'string' ? data.callId : '') as CallId,
            content,
            isError: data.is_error === true,
          }],
          source: { kind: 'tool', callId: (typeof data.callId === 'string' ? data.callId : '') as CallId },
        }
        out.push(mk('tool/result', { turn, step, message, surfaceOp: data.surfaceOp } as Record<string, unknown>)); break
      }
      case 'request/header': {
        const header = (data.header ?? {}) as Record<string, unknown>
        out.push(mk('request/header', {
          header: {
            config: (header.config ?? {}) as never,
            system: typeof header.system === 'string' ? header.system : undefined,
            tools: (header.tools ?? []) as never[],
          },
          reason: ['resume', 'change'].includes(String(data.reason))
            ? data.reason
            : 'initial',
        } as Record<string, unknown>)); break
      }
      case 'request/context':
        out.push(mk('request/context', { context: data.context ?? null, reason: data.reason ?? 'change' })); break
      case 'todo/write':
        out.push(mk('todo/write', { todos: (data.todos ?? []) as never[] })); break
      default:
        // dsh 词汇外(session/start、session/end、error、context/inject、context/modify)→ 丢弃
        break
    }
    return out
  }
}
