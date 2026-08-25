// 会话列表:搜索 + 状态筛选 + 相对时间;点击选中会话
import { useMemo } from 'react';
import { StateBadge, useLanguage } from '@gh-puller/ui';
import type { SessionMeta, SessionState } from '../types';

interface Props {
  sessions: SessionMeta[];
  current: string | null;
  query: string;
  stateFilter: SessionState | 'all';
  onSelect: (session: string) => void;
}

const fmtTime = (ts: number) => new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false });

export default function SessionList({ sessions, current, query, stateFilter, onSelect }: Props) {
  const { t } = useLanguage();
  const q = query.trim().toLowerCase();
  const items = useMemo(
    () => sessions.filter((s) =>
      (stateFilter === 'all' || s.state === stateFilter) &&
      (!q || `${s.session} ${s.label} ${s.provider} ${s.model}`.toLowerCase().includes(q))
    ),
    [sessions, q, stateFilter],
  );

  if (!items.length) {
    return <div className="p-4 text-xs text-zinc-500 dark:text-zinc-600">{t('sidebar.empty')}</div>;
  }
  return (
    <ul className="space-y-1 p-2">
      {items.map((s) => (
        <li key={s.session}>
          <button
            onClick={() => onSelect(s.session)}
            className={`w-full rounded-md border-l-2 px-3 py-2 text-left transition-colors ${
              s.session === current
                ? 'border-emerald-500 bg-emerald-500/10'
                : 'border-transparent hover:bg-zinc-800/40'
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-sm text-zinc-800 dark:text-zinc-200">{s.label}</span>
              <StateBadge state={s.state} label={t(`session.state.${s.state}`)} />
            </div>
            <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-zinc-500 dark:text-zinc-600">
              <span className="truncate font-mono">{s.provider}/{s.model || '—'}</span>
              <span className="shrink-0 font-mono">{fmtTime(s.last_ts)}</span>
            </div>
          </button>
        </li>
      ))}
    </ul>
  );
}
