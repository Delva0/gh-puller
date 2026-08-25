// 轨迹表格布局(仿 dsh ui-trajectory/src/client/layout.ts 的 turn/step 归组):
// gh-puller 单 run = 单 turn,按 step(一次 LLM 请求)归组单元格。

import type { ChatNode, RequestView, ToolCallView } from './types';

export interface LayoutCell {
  key: string;
  kind: 'request' | 'tool' | 'context' | 'system';
  step: number;
  seq: number;
  request?: RequestView;
  tool?: ToolCallView;
  callId?: string;
  text?: string;
  isError?: boolean;
}

export interface LayoutGroup {
  key: string;
  step: number;
  label: string; // "step N" / "LLM 请求 N"
  cells: LayoutCell[];
}

/** 请求序 → 分组(按 step)与单元格(请求格 + 工具格 + 系统/上下文格)。 */
export function deriveTrajectoryLayout(
  requests: RequestView[],
  nodes: ChatNode[],
): LayoutGroup[] {
  const groups = new Map<number, LayoutGroup>();
  const ensure = (step: number): LayoutGroup => {
    let g = groups.get(step);
    if (!g) {
      g = { key: `step-${step}`, step, label: `step ${step}`, cells: [] };
      groups.set(step, g);
    }
    return g;
  };
  for (const req of requests) {
    ensure(req.step).cells.push({ key: `req-${req.seq}`, kind: 'request', step: req.step, seq: req.seq, request: req });
    for (const t of req.tools) {
      ensure(t.step).cells.push({
        key: `tool-${t.callId}`, kind: 'tool', step: t.step, seq: t.seq, tool: t, callId: t.callId, isError: t.isError,
      });
    }
  }
  for (const n of nodes) {
    if (n.kind === 'context') {
      ensure(n.step).cells.push({ key: `ctx-${n.seq}`, kind: 'context', step: n.step, seq: n.seq, text: n.contextText });
    } else if (n.kind === 'system') {
      ensure(n.step).cells.push({ key: `sys-${n.seq}`, kind: 'system', step: n.step, seq: n.seq, text: `config → ${n.header?.config?.model ?? ''}` });
    }
  }
  return [...groups.values()].sort((a, b) => a.step - b.step);
}
