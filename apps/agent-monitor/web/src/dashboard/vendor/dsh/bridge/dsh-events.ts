/**
 * gh-puller 事件流 → dsh SessionEvent 适配器(桥接层唯一新写的数据代码)。
 *
 * gh 侧事件分类学(gh_puller/agent/events.py,协议最终标准)与 dsh 事件词表本就同构
 * ("对齐 dsh 核心不变量");差异点与处理:
 * - `assistant/chunk`:gh 为 content/thinking/tool_call 增量,dsh 为 block-start /
 *   *-delta / block-end 语言 → 按 (turn,step,index) 状态机合成;
 * - `assistant/message`:gh 一 step 发多条段消息 —— 协议语义(events.py 折叠恢复
 *   规范 + generators.py)为同一 step 消息的时间快照(sourceSeqs 均覆盖全步 chunk
 *   集;空 content 段派生 None,仅 usage 终结标记),而 dsh 一步一行且对
 *   assistant/message 是替换语义 → 按 (turn,step) 合并缓冲,步边界统一吐出
 *   一条全量消息(锚=首条非空段消息 seq)再吐本步工具事件,seq 全局单调;
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
import type { ContentBlock, TokenUsage, ToolResultBlock } from '../llm/types.ts'
import type { CallId, MessageId } from '../llm/brand.ts'

const BASE = 'ghp'

/**
 * 单个 gh assistant/chunk 会展开成 block-start + delta 两个 dsh 事件,而两者共用
 * gh 的 seq —— 违反 dsh 事件流"每事件独占严格递增 seq"契约(assembler 对同
 * context 断言 non-appended 并抛错,整窗折叠失败 → 面板空白)。block-start 取该
 * gh 事件 seq 以下 0.25 的分数槽(dsh 自身的合成节点序也用分数偏移,见
 * CHAT_SYNTHETIC_SEQ_OFFSETS),delta 仍锚定 gh seq —— 全局单调且可追源。
 */
export const BLOCK_START_SEQ_OFFSET = -0.25

/** dsh 需要 id 归属的消息 id(gh 无);从事件 id 派生。 */
function msgId(seed: string): MessageId {
  return `${BASE}:msg:${seed}` as MessageId
}

/** `tool/call` id 缺失时合成的 chunk 关联 id。 */
function chunkCallId(turn: number, step: number, index: number): CallId {
  return `${BASE}:chunk:${turn}.${step}.${index}` as CallId
}

/**
 * gh 内容块 → dsh ContentBlock。
 * 词表以 gh 协议为准(gh_puller/agent/generators.py):assistant 消息块 =
 * content{text}(可见文本)/ thinking{text} / tool_call{id,name,input}
 * (input 为 dict 或 JSON 字符串);tool/result 消息块 = tool_result
 * {tool_use_id,content,is_error};`text`/`tool_use` 为历史 legacy,保留兼容。
 */
function toBlocks(message: GhMessage): ContentBlock[] {
  const blocks: ContentBlock[] = []
  for (const block of message.content) {
    const raw = block as Record<string, unknown>
    const type = raw.type
    if ((type === 'text' || type === 'content') && typeof raw.text === 'string') {
      blocks.push({ type: 'text', text: raw.text })
    } else if (type === 'thinking' && typeof raw.text === 'string') {
      blocks.push({ type: 'reasoning', text: raw.text })
    } else if (type === 'tool_call' || type === 'tool_use') {
      blocks.push({
        type: 'tool-call',
        id: (typeof raw.id === 'string' ? raw.id : '') as CallId,
        name: typeof raw.name === 'string' ? raw.name : '',
        arguments: typeof raw.input === 'string' ? raw.input : JSON.stringify(raw.input ?? {}),
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

/** 步级合并吸收的事件:同 (turn,step) 的四类直入缓冲/直发,其余事件先冲刷。 */
const STEP_TYPES: ReadonlySet<string> = new Set(['assistant/message', 'assistant/chunk', 'tool/call', 'tool/result'])

type DshEvent = SessionEvent

/**
 * (turn,step) 步级合并缓冲。gh 一 step 多条段消息(协议语义:同一 step 消息的
 * 时间快照,sourceSeqs 均覆盖全步 chunk 集),dsh 一步一行且 assistant/message
 * 为替换语义 → 合并为一条全量消息,锚=首条非空段消息的 seq(行序=锚序,该步
 * 行位于步首、工具行之前);本步工具事件保留各自 seq 于步末按序吐出。
 */
interface StepMerge {
  key: string
  turn: number
  step: number
  blocks: ContentBlock[]
  anchorSeq?: number
  anchorTime?: number
  anchorId?: string
  anchorSurfaceOp?: unknown
  usage?: TokenUsage
  interrupted: boolean
  buffered: DshEvent[]
}

/** 合成流式 chunk 的状态机:同一 (turn,step,index) 只发一次 block-start。 */
export class GhToDshEvents {
  private readonly starts = new Map<string, boolean>()
  private merge: StepMerge | null = null

  reset(): void {
    this.starts.clear()
    this.merge = null
  }

  /** 冲刷当前步合并:先合并助手消息(如有),再按原 seq 吐缓冲的工具事件。 */
  private flush(): DshEvent[] {
    const m = this.merge
    this.merge = null
    if (m === null) return []
    const out: DshEvent[] = []
    if (m.anchorSeq !== undefined && m.blocks.length > 0) {
      const message = {
        id: msgId(m.anchorId ?? `${m.turn}:${m.step}`),
        role: 'assistant',
        content: [...m.blocks],
        source: { kind: 'model', provider: 'provider', model: '', callId: '', purpose: 'generate' },
      } as AssistantMessage
      const merged = {
        type: 'assistant/message' as const,
        seq: m.anchorSeq,
        time: m.anchorTime ?? 0,
        surfaceOp: typeof m.anchorSurfaceOp === 'string'
          ? m.anchorSurfaceOp
          : ((m.anchorSurfaceOp as { op?: string } | null | undefined)?.op
            ? (m.anchorSurfaceOp as { op: string; start: number; end: number })
            : 'append'),
        data: {
          turn: m.turn,
          step: m.step,
          message,
          ...(m.usage === undefined ? {} : { usage: m.usage }),
          ...(m.interrupted ? { interrupted: true as const } : {}),
        },
      }
      out.push(merged as DshEvent)
    }
    if (m.buffered.length > 0) {
      out.push(...[...m.buffered].sort((a, b) => a.seq - b.seq))
    }
    return out
  }

  private ensureMerge(turn: number, step: number, key: string): StepMerge {
    if (this.merge === null) {
      this.merge = { key, turn, step, blocks: [], interrupted: false, buffered: [] }
    }
    return this.merge
  }

  /** 翻译单个 gh 信封;返回 0..n 个 dsh 事件(多数 1)。 */
  translate(evt: EventEnvelope): DshEvent[] {
    const data = evt.data as Record<string, unknown>
    const num = (v: unknown): number | undefined => (typeof v === 'number' ? v : undefined)
    const turn = num(data.turn) ?? 1
    const step = num(data.step) ?? 1
    const key = `${turn}:${step}`
    const mk = (type: DshEvent['type'], payload: Record<string, unknown>, seq?: number): DshEvent => {
      const event: Record<string, unknown> = {
        type: type as DshEvent['type'],
        seq: seq ?? evt.seq,
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

    // 步级合并边界:非本步四类事件(含 step/end、换步)触达 → 先冲刷缓步。
    const sameStep = this.merge !== null && this.merge.key === key
    if (this.merge !== null && !(STEP_TYPES.has(evt.type) && sameStep)) {
      out.push(...this.flush())
    }

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
      case 'assistant/message': {
        // 段消息并入当前步合并(空 content 段仅吸收 usage/interrupted)。
        const m = this.ensureMerge(turn, step, key)
        const blocks = toBlocks(data.message as GhMessage)
        if (blocks.length > 0) {
          if (m.anchorSeq === undefined) {
            m.anchorSeq = evt.seq
            m.anchorTime = Math.round(evt.ts * 1000)
            m.anchorId = evt.id
            m.anchorSurfaceOp = data.surfaceOp
          }
          m.blocks.push(...blocks)
        }
        const u = usage(data.usage)
        if (u !== undefined) m.usage = u
        if (data.interrupted === true) m.interrupted = true
        break
      }
      case 'assistant/chunk': {
        const chunk = data.chunk as { type?: string; index?: number; text?: string; partial_json?: string }
        const index = num(chunk.index) ?? 0
        const chunkKey = `${turn}:${step}:${index}`
        const blockType: 'text' | 'reasoning' | 'tool-call' = chunk.type === 'thinking' ? 'reasoning'
          : (chunk.type === 'tool_call' || chunk.type === 'tool_input') ? 'tool-call' : 'text'
        if (!this.starts.has(chunkKey)) {
          this.starts.set(chunkKey, true)
          out.push(mk('assistant/chunk', { turn, step, chunk: { type: 'block-start', index, blockType } },
            evt.seq + BLOCK_START_SEQ_OFFSET))
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
      case 'tool/call': {
        const m = this.ensureMerge(turn, step, key)
        m.buffered.push(mk('tool/call', {
          turn, step,
          callId: (typeof data.callId === 'string' ? data.callId : '') as CallId,
          name: typeof data.name === 'string' ? data.name : '',
          arguments: typeof data.arguments === 'string' ? data.arguments : '{}',
        }))
        break
      }
      case 'tool/result': {
        const m = this.ensureMerge(turn, step, key)
        const ghMsg = data.message as GhMessage | null | undefined
        const inner = ghMsg == null ? [] : toBlocks(ghMsg)
        // gh tool/result 消息块已是 tool_result → toBlocks 产出单个 tool-result 块,
        // 直接作为消息 content 的唯一块(避免 tool-result 嵌套包裹)。
        const message: ToolResultMessage = {
          id: msgId(evt.id),
          role: 'user',
          content: [inner[0] ?? {
            type: 'tool-result',
            toolCallId: (typeof data.callId === 'string' ? data.callId : '') as CallId,
            content: [{ type: 'text', text: '' }],
            isError: data.is_error === true,
          } as ToolResultBlock] as [ToolResultBlock],
          source: { kind: 'tool', callId: (typeof data.callId === 'string' ? data.callId : '') as CallId },
        }
        m.buffered.push(mk('tool/result', { turn, step, message, surfaceOp: data.surfaceOp } as Record<string, unknown>))
        break
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
