'use client';

// 对话视图:节点序列(user/assistant/工具结果/上下文/系统卡/尾部)+ 流式 partial 接续;
// 自动跟随滚动(仿 dsh ChatView 的 to-bottom 语义,gh-puller v2 首版用 autoScroll 开关)

import { useEffect, useRef } from 'react';
import { useLanguage } from '../contexts/LanguageContext';
import type { ChatNode, ToolCallView } from '../monitor';
import MonitorNodeSeat from './MonitorNodeSeat';

interface Props {
  nodes: ChatNode[];
  /** 尾部流式中的未定型文本(最后一条 assistant/message 之后) */
  partial: string;
  toolsByCall: Map<string, ToolCallView>;
  autoScroll: boolean;
}

export default function MonitorChatView({ nodes, partial, toolsByCall, autoScroll }: Props) {
  const { t } = useLanguage();
  const ref = useRef<HTMLDivElement>(null);
  const len = nodes.length;
  const partialLen = partial.length;

  useEffect(() => {
    if (autoScroll && ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  }, [autoScroll, len, partialLen]);

  if (!nodes.length && !partial) {
    return <div className="p-6 text-xs text-[var(--muted)]">{t('view.empty')}</div>;
  }

  return (
    <div ref={ref} className="h-full overflow-y-auto px-4 py-3">
      <div className="mx-auto max-w-3xl space-y-3">
        {nodes.map((n) => (
          <MonitorNodeSeat key={`${n.kind}-${n.seq}`} node={n} toolsByCall={toolsByCall} />
        ))}
        {partial && (
          <div className="rounded-md border border-[var(--border-color)] bg-[var(--card-bg)] px-3 py-2">
            <div className="mb-1 font-mono text-[10px] text-[var(--muted)]">{t('view.streaming')}</div>
            <div className="whitespace-pre-wrap text-sm text-[var(--foreground)]">{partial}</div>
          </div>
        )}
        {partialLen === 0 && nodes.length > 0 && <div className="h-2" />}
      </div>
    </div>
  );
}
