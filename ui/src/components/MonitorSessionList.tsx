'use client';

// 监控侧栏会话列表:搜索/状态筛选 + run_id 分组头(任务级会话组,组头带条数)

import { useMemo } from 'react';
import StateBadge from './StateBadge';
import { useLanguage } from '../contexts/LanguageContext';
import type { SessionMeta } from '../monitor';

interface Props {
  sessions: SessionMeta[];
  current: string | null;
  onSelect: (session: string) => void;
  query: string;
  stateFilter: string;
}

const fmt = (ts: number) =>
  ts > 0
    ? new Date(ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : '';

export default function MonitorSessionList({ sessions, current, onSelect, query, stateFilter }: Props) {
  const { t } = useLanguage();
  const rows = useMemo(
    () =>
      sessions.filter((s) => {
        if (stateFilter !== 'all' && s.state !== stateFilter) return false;
        const q = query.trim().toLowerCase();
        if (!q) return true;
        return `${s.label} ${s.session} ${s.run_id ?? ''}`.toLowerCase().includes(q);
      }),
    [sessions, query, stateFilter],
  );

  if (!rows.length) {
    return <div className="px-3 py-6 text-center text-xs text-[var(--muted)]">{t('sidebar.empty')}</div>;
  }

  return (
    <ul>
      {rows.map((s, i) => (
        <li key={s.session}>
          {Boolean(s.run_id && s.run_id !== rows[i - 1]?.run_id) && (
            <div className="flex items-center gap-1.5 px-3 pt-2 pb-0.5 font-mono text-[10px] text-[var(--muted)]">
              <span className="truncate">{s.run_id}</span>
              <span className="text-[var(--accent-primary)]">
                {rows.filter((x) => x.run_id === s.run_id).length}
              </span>
            </div>
          )}
          <button
            type="button"
            onClick={() => onSelect(s.session)}
            className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-gray-500/10 ${
              current === s.session ? 'bg-[var(--accent-primary)]/10' : ''
            }`}
          >
            <span className="flex-1 truncate font-mono">{s.label}</span>
            {s.provider && (
              <span className="font-mono text-[10px] text-[var(--muted)]">{s.provider}/{s.model || '—'}</span>
            )}
            <StateBadge state={s.state} label={t(`session.state.${s.state}`)} />
            <span className="font-mono text-[10px] text-[var(--muted)]">{fmt(s.last_ts)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
