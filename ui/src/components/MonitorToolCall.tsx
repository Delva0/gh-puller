'use client';

// 工具调用卡:调用名(chip)+ 原始 JSON 参数 + 结果全文;结果可折叠(超长滚动)。
// 输入输出全量不截断(事件溯源承诺),仅布局上折叠防滚动爆炸

import { useMemo, useState } from 'react';
import { useLanguage } from '../contexts/LanguageContext';
import type { ToolCallView } from '../monitor';

interface Props {
  call: ToolCallView;
}

function pretty(value: string | undefined): string {
  if (!value) return '';
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

export default function MonitorToolCall({ call }: Props) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const argText = useMemo(() => pretty(call.arguments), [call.arguments]);

  return (
    <div className="rounded-md border border-[var(--border-color)] bg-[var(--card-bg)] px-2.5 py-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 text-left"
      >
        <span className="text-[var(--highlight)]">🔧</span>
        <span className="truncate font-mono text-xs text-[var(--foreground)]">{call.name}</span>
        <span className="truncate font-mono text-[10px] text-[var(--muted)]">{call.callId}</span>
        {call.isError && (
          <span className="rounded bg-red-500/15 px-1 font-mono text-[10px] text-red-500">
            {t('view.errorLabel')}
          </span>
        )}
        <span className="ml-auto text-[var(--muted)]">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5">
          {argText && (
            <pre className="max-h-48 overflow-auto rounded border border-[var(--border-color)] bg-black/10 p-1.5 font-mono text-[11px] text-[var(--foreground)]">
              {argText}
            </pre>
          )}
          {call.result !== undefined && (
            <pre
              className={`max-h-48 overflow-auto rounded border p-1.5 font-mono text-[11px] whitespace-pre-wrap ${
                call.isError
                  ? 'border-red-500/30 bg-red-500/10 text-red-500'
                  : 'border-[var(--border-color)] bg-black/10 text-[var(--foreground)]'
              }`}
            >
              {call.result || (call.isError ? t('view.emptyResult') : '')}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
