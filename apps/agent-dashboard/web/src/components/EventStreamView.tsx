// 原始事件视图:kind 筛选 + 单行摘要(kind·ts·关键字段)→ 点击展开 JSON(排查用)
import { useState } from 'react';
import { useLanguage } from '@gh-puller/ui';
import { EVENT_KINDS } from '../types';

interface Props {
  events: Record<string, unknown>[];
}

const KIND_CLASS: Record<string, string> = {
  'run.start': 'text-sky-500',
  'run.end': 'text-emerald-500',
  'error': 'text-red-500',
  'result': 'text-violet-500',
  'message.assistant': 'text-violet-500',
};

const fmtTs = (ts?: unknown) =>
  typeof ts === 'number' ? new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '';

// 单行摘要:取事件首个有意义的字符串字段(文本/消息/预览)
const summaryOf = (evt: Record<string, unknown>): string => {
  for (const k of ['text', 'message', 'content_preview', 'exc_type', 'tool_name']) {
    const v = evt[k];
    if (typeof v === 'string' && v) return v.length > 60 ? v.slice(0, 60) + '…' : v;
  }
  return '';
};

export default function EventStreamView({ events }: Props) {
  const { t } = useLanguage();
  const [kindFilter, setKindFilter] = useState<string>('all');
  const [openId, setOpenId] = useState<string | null>(null);

  if (!events.length) {
    return <div className="p-6 text-xs text-zinc-500 dark:text-zinc-600">{t('events.empty')}</div>;
  }

  const kindOptions = ['all', ...EVENT_KINDS.filter((k) => events.some((e) => e.kind === k))];
  const items = kindFilter === 'all' ? events : events.filter((e) => e.kind === kindFilter);

  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <label className="text-xs text-zinc-500">{t('sidebar.filter')}</label>
        <select
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
          className="rounded-md border border-zinc-300 bg-transparent px-2 py-1 text-xs text-zinc-700 dark:border-zinc-700 dark:text-zinc-300"
        >
          {kindOptions.map((k) => (
            <option key={k} value={k}>{k === 'all' ? t('events.filter.all') : k}</option>
          ))}
        </select>
        <span className="text-[11px] text-zinc-500">{items.length} / {events.length}</span>
      </div>
      <ul className="space-y-1">
        {items.map((evt, idx) => {
          const id = typeof evt.id === 'string' ? evt.id : `${evt.kind}-${idx}`;
          const open = openId === id;
          return (
            <li key={id} className="rounded-md border border-zinc-200 dark:border-zinc-800">
              <button
                onClick={() => setOpenId(open ? null : id)}
                className="flex w-full items-baseline gap-2 px-3 py-1.5 text-left hover:bg-zinc-800/30"
              >
                <span className={`shrink-0 font-mono text-xs font-bold ${KIND_CLASS[String(evt.kind)] ?? 'text-zinc-500'}`}>
                  {String(evt.kind)}
                </span>
                <span className="shrink-0 font-mono text-[11px] text-zinc-500">{fmtTs(evt.ts)}</span>
                <span className="truncate text-xs text-zinc-500 dark:text-zinc-400">{summaryOf(evt)}</span>
              </button>
              {open && (
                <pre className="overflow-x-auto border-t border-zinc-200 px-3 py-2 font-mono text-xs text-zinc-600 dark:border-zinc-800 dark:text-zinc-300">
                  {JSON.stringify(evt, null, 2)}
                </pre>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
