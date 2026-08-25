'use client';

// 节点座位:按 kind 分发到对应渲染器(仿 dsh ChatNodeSeat 的键控分发表;gh-puller
// 用内置 switch,不做可插拔槽)

import type { ChatNode, ToolCallView } from '../monitor';
import MonitorContextRow from './MonitorContextRow';
import MonitorMessageItem from './MonitorMessageItem';
import MonitorToolCall from './MonitorToolCall';
import MonitorTurnTail from './MonitorTurnTail';

interface Props {
  node: ChatNode;
  toolsByCall: Map<string, ToolCallView>;
}

export default function MonitorNodeSeat({ node, toolsByCall }: Props) {
  switch (node.kind) {
    case 'user':
    case 'assistant':
      return <MonitorMessageItem node={node} />;
    case 'tool-result': {
      const call = node.callId ? toolsByCall.get(node.callId) : undefined;
      if (call) return <MonitorToolCall call={{ ...call, result: node.contextText, isError: node.contextKind === 'error' }} />;
      return <MonitorToolCall call={{ callId: node.callId ?? '', name: node.name, step: node.step, seq: node.seq, result: node.contextText }} />;
    }
    case 'context':
      return <MonitorContextRow text={node.contextText ?? ''} kind={node.contextKind ?? ''} />;
    case 'system':
      return <MonitorContextRow text={systemText(node)} kind="system" />;
    case 'turn-tail':
      return <MonitorTurnTail node={node} />;
    default:
      return null;
  }
}

function systemText(node: ChatNode): string {
  const h = node.header;
  if (!h) return '';
  const model = (h.config as { model?: string } | undefined)?.model;
  const partial = node.contextKind === 'header-partial' ? ' (partial)' : '';
  return `config: ${h.config ? JSON.stringify(h.config) : ''}${model ? ` · model ${model}` : ''}${partial}`;
}
