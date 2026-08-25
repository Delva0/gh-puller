'use client';

// 消息行:用户气泡(右对齐,主题强调色)/ 助手卡片(thinking 折叠 + Markdown 正文 +
// tool_use 块芯片);markdown 复用共享 Markdown 组件

import { useState } from 'react';
import Markdown from './Markdown';
import { useLanguage } from '../contexts/LanguageContext';
import type { ChatNode } from '../monitor';

interface Props {
  node: ChatNode;
}

export default function MonitorMessageItem({ node }: Props) {
  const { t } = useLanguage();
  const [showThinking, setShowThinking] = useState(false);
  const msg = node.message;
  if (!msg) return null;
  const isUser = node.kind === 'user';

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-md rounded-br-sm bg-[var(--accent-primary)]/15 px-3 py-2">
          <div className="whitespace-pre-wrap text-sm text-[var(--foreground)]">{textOf(msg)}</div>
        </div>
      </div>
    );
  }

  const thinking = msg.content.filter((b) => (b as { type?: string }).type === 'thinking');
  const tools = msg.content.filter((b) => (b as { type?: string }).type === 'tool_use');
  return (
    <div className="rounded-md border border-[var(--border-color)] bg-[var(--card-bg)] px-3 py-2">
      {thinking.length > 0 && (
        <button
          type="button"
          onClick={() => setShowThinking((v) => !v)}
          className="mb-1.5 flex items-center gap-1 font-mono text-[10px] text-[var(--muted)] hover:text-[var(--foreground)]"
        >
          <span>{t('view.thinkingLabel')} ({thinking.length})</span>
          <span>{showThinking ? '▾' : '▸'}</span>
        </button>
      )}
      {showThinking &&
        thinking.map((b, i) => (
          <div key={i} className="mb-2 rounded border-l-2 border-[var(--accent-secondary)] bg-black/10 px-2 py-1">
            <pre className="whitespace-pre-wrap font-mono text-xs text-[var(--muted)]">
              {String((b as { thinking?: string }).thinking ?? '')}
            </pre>
          </div>
        ))}
      <Markdown content={textOf(msg)} />
      {tools.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {tools.map((b, i) => (
            <span
              key={i}
              className="rounded border border-[var(--accent-primary)]/40 px-1.5 py-0.5 font-mono text-[10px] text-[var(--accent-primary)]"
            >
              {String((b as { name?: string }).name ?? '')}
              {String((b as { id?: string }).id ?? '') && ` · ${String((b as { id?: string }).id ?? '')}`}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function textOf(msg: { content: Array<Record<string, unknown>> }): string {
  return msg.content
    .map((b) => b.text ?? b.thinking ?? '')
    .filter((x) => typeof x === 'string')
    .join('\n');
}
