'use client';

// 上下文行:注入/修改/系统快照的细条(仿 dsh ContextInjectionRow):
// 标签片 + 可折叠全文(上下文注入与修改事件的解释性展示)

import { useState } from 'react';
import { useLanguage } from '../contexts/LanguageContext';

interface Props {
  text: string;
  kind: string; // inject | trim | degrade | replace | system | ...
}

const ICON: Record<string, string> = {
  inject: '⤷',
  trim: '✂',
  degrade: '⚠',
  replace: '↻',
  system: '⚙',
};

export default function MonitorContextRow({ text, kind }: Props) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const label = t(`context.${kind}`, {});
  const long = text.length > 200;

  return (
    <div className="rounded border border-dashed border-[var(--accent-secondary)] px-2.5 py-1.5">
      <button type="button" onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-1.5 text-left">
        <span className="text-[var(--accent-secondary)]">{ICON[kind] ?? '·'}</span>
        <span className="font-mono text-[10px] uppercase tracking-wide text-[var(--muted)]">{label}</span>
        {(long || open) && <span className="ml-auto text-[var(--muted)]">{open ? '▾' : '▸'}</span>}
      </button>
      {(open || !long) && (
        <div className="mt-1 whitespace-pre-wrap text-xs text-[var(--muted)]">{text}</div>
      )}
    </div>
  );
}
