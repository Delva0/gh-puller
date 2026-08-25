'use client';

// 轨迹工具栏:时间轴模式切换(序列/时长/时间/实际)+ 折叠全部 + 搜索
// (仿 dsh TrajectoryToolbar;gh-puller 无 subagent/queue,只剩核心项)

import { useLanguage } from '../contexts/LanguageContext';
import type { TimelineMode } from '../monitor';

interface Props {
  mode: TimelineMode;
  onMode: (m: TimelineMode) => void;
  onCollapseAll: () => void;
  query: string;
  onQuery: (q: string) => void;
}

const MODES: Array<{ id: TimelineMode; key: string }> = [
  { id: 'sequence', key: 'traj.mode.sequence' },
  { id: 'duration', key: 'traj.mode.duration' },
  { id: 'time', key: 'traj.mode.time' },
  { id: 'actual', key: 'traj.mode.actual' },
];

export default function MonitorTrajectoryToolbar({ mode, onMode, onCollapseAll, query, onQuery }: Props) {
  const { t } = useLanguage();
  return (
    <div className="flex flex-wrap items-center gap-2 px-4 py-2">
      <div className="flex overflow-hidden rounded-md border border-[var(--border-color)] text-[11px]">
        {MODES.map((m) => (
          <button
            key={m.id}
            type="button"
            onClick={() => onMode(m.id)}
            className={`px-2 py-1 transition-colors ${
              mode === m.id
                ? 'bg-[var(--accent-primary)]/15 text-[var(--accent-primary)]'
                : 'text-[var(--muted)] hover:text-[var(--foreground)]'
            }`}
          >
            {t(m.key)}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={onCollapseAll}
        className="rounded-md border border-[var(--border-color)] px-2 py-1 text-[11px] text-[var(--muted)] hover:border-[var(--accent-primary)]"
      >
        {t('traj.collapseAll')}
      </button>
      <input
        value={query}
        onChange={(e) => onQuery(e.target.value)}
        placeholder={t('traj.search')}
        className="rounded-md border border-[var(--border-color)] bg-transparent px-2 py-1 text-xs placeholder:text-[var(--muted)] focus:border-[var(--accent-primary)] focus:outline-none"
      />
    </div>
  );
}
