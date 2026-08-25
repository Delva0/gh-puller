'use client';

// 尾部行:重试元信息(琥珀)/ 错误(红)分隔;对应 dsh 的 TurnTail + TurnErrorNodeView

import { useLanguage } from '../contexts/LanguageContext';
import type { ChatNode } from '../monitor';

interface Props {
  node: ChatNode;
}

export default function MonitorTurnTail({ node }: Props) {
  const { t } = useLanguage();
  if (node.retry) {
    const r = node.retry;
    return (
      <div className="rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-500">
        {t('view.retryLabel', { attempt: r.attempt })}
        {r.prev_error ? ` · ${r.prev_error.slice(0, 200)}` : ''}
      </div>
    );
  }
  if (node.contextKind === 'error' && node.contextText) {
    return (
      <div className="rounded border border-red-500/30 bg-red-500/10 px-2.5 py-1.5 text-xs text-red-500">
        <span className="font-mono">{node.contextText}</span>
      </div>
    );
  }
  if (node.kind === 'turn-tail' && node.name) {
    // session/start 行(顶部):run 标签
    return (
      <div className="font-mono text-[10px] text-[var(--muted)]">
        {t('view.sessionStart', { label: node.name })}
      </div>
    );
  }
  return null;
}
