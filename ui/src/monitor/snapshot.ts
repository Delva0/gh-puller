// 事件流 → 界面快照(仿 dsh runtime/sessions + trajectory-snapshot-builder):
// 单遍按 seq 归并出对话节点序与请求序(轨迹表输入),含流式 partial 与在途工具。

import { contextProvenance } from './provenance';
import type {
  ChatNode, ChunkData, ContextInjectData, ContextModifyData, EventEnvelope, HeaderData,
  RequestView, Snapshot, SourceInfo, ToolCallView, Usage,
} from './types';

export function buildSnapshot(events: EventEnvelope[]): Snapshot {
  const chatNodes: ChatNode[] = [];
  const requests = new Map<number, RequestView>(); // step → 请求(每个 step 一条)
  const calls = new Map<string, ToolCallView>();
  const order: EventEnvelope[] = [...events].sort((a, b) => a.seq - b.seq);

  const reqOf = (step: unknown, seq: number): RequestView => {
    const s = Number(step ?? 1);
    let r = requests.get(s);
    if (!r) {
      r = { seq, step: s, turn: 1, text: '', thinking: '', tools: [] };
      requests.set(s, r);
    }
    return r;
  };

  for (const evt of order) {
    const d = evt.data as Record<string, unknown>;
    const step = Number(d.step ?? 1);
    const turn = Number(d.turn ?? 1);
    switch (evt.type) {
      case 'session/start': {
        const retry = (evt.data as { retry?: RequestView['retry'] }).retry;
        chatNodes.push({ seq: evt.seq, kind: 'turn-tail', turn: 1, step: 1, retry, name: evt.label });
        break;
      }
      case 'turn/start':
      case 'step/start':
        break;
      case 'user/message': {
        const m = d.message as ChatNode['message'];
        const p = contextProvenance(d.source as unknown as SourceInfo | undefined);
        if (p.kind === 'context') {
          chatNodes.push({ seq: evt.seq, kind: 'context', turn, step, contextText: textOf(m), contextKind: p.form });
        } else {
          chatNodes.push({ seq: evt.seq, kind: 'user', turn, step, message: m });
        }
        break;
      }
      case 'assistant/chunk': {
        const c = d.chunk as ChunkData['chunk'];
        const req = reqOf(step, evt.seq);
        if (req.seq > evt.seq) req.seq = evt.seq; // 首个 chunk 即请求平面
        req.ts ??= evt.ts;
        if (c.type === 'text') req.text += c.text ?? '';
        if (c.type === 'thinking') req.thinking += c.text ?? '';
        // 对话视图的流式部分:由快照尾部 partial 承载(见下方 partialText)
        break;
      }
      case 'assistant/message': {
        const m = d.message as ChatNode['message'];
        const req = reqOf(step, evt.seq);
        req.usage = (d as { usage?: Usage | null }).usage ?? req.usage;
        req.stopReason = (d as { stop_reason?: string | null }).stop_reason ?? req.stopReason;
        if (d.interrupted) req.interrupted = true;
        chatNodes.push({ seq: evt.seq, kind: 'assistant', turn, step, message: m });
        break;
      }
      case 'tool/call': {
        const t: ToolCallView = {
          callId: (d as { callId: string }).callId,
          name: d.name as string | undefined,
          arguments: d.arguments as string,
          seq: evt.seq,
          step,
        };
        calls.set(t.callId, t);
        reqOf(step, evt.seq).tools.push(t);
        break;
      }
      case 'tool/result': {
        const callId = (d as { callId?: string }).callId;
        const t = callId ? calls.get(callId) : undefined;
        if (t) {
          t.result = textOf(d.message as ChatNode['message']);
          t.isError = Boolean(d.is_error);
          t.resultSeq = evt.seq;
        }
        reqOf(step, evt.seq);
        chatNodes.push({ seq: evt.seq, kind: 'tool-result', turn, step, callId, name: d.name as string | undefined, contextText: textOf(d.message as ChatNode['message']), contextKind: 'tool_result' });
        break;
      }
      case 'context/inject': {
        const di = d as unknown as ContextInjectData;
        chatNodes.push({ seq: evt.seq, kind: 'context', turn, step, contextText: di.text, contextKind: 'inject' });
        break;
      }
      case 'context/modify': {
        const dm = d as unknown as ContextModifyData;
        chatNodes.push({ seq: evt.seq, kind: 'context', turn, step, contextText: dm.detail ?? dm.kind, contextKind: dm.kind });
        break;
      }
      case 'request/header': {
        const h = (d as unknown as HeaderData).header;
        chatNodes.push({ seq: evt.seq, kind: 'system', turn, step, header: h });
        break;
      }
      case 'error': {
        const e = d as { message?: string };
        reqOf(step, evt.seq).error = e.message ?? '';
        chatNodes.push({ seq: evt.seq, kind: 'turn-tail', turn, step, contextText: e.message, contextKind: 'error' });
        break;
      }
      default:
        break; // turn/step/lifecycle 等:对话/轨迹均不渲染
    }
  }

  // 尾部 partial:最后一条 assistant/message 之后的 text chunk(对话视图流式接续)
  let msgSeq = -1;
  for (const e of order) {
    if (e.type === 'assistant/message') msgSeq = e.seq;
  }
  let partial = '';
  for (const e of order) {
    if (e.type !== 'assistant/chunk') continue;
    const c = e.data.chunk as ChunkData['chunk'];
    if (c.type === 'text' && e.seq > msgSeq) partial += c.text ?? '';
  }

  const runningCalls = [...calls.values()].filter((t) => t.resultSeq === undefined);
  return {
    chatNodes,
    requests: [...requests.values()].sort((a, b) => a.seq - b.seq),
    runningCalls,
    partial,
  };
}

function textOf(m?: ChatNode['message']): string {
  if (!m) return '';
  return m.content
    .map((b) => {
      const b2 = b as Record<string, unknown>;
      if (typeof b2.text === 'string') return b2.text;
      if (typeof b2.thinking === 'string') return b2.thinking;
      if (typeof b2.content === 'string') return b2.content; // tool_result 块
      return '';
    })
    .join('');
}
