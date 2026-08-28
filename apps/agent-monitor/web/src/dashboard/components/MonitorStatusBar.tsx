'use client';

// 底部状态栏:连接状态 + 当前会话 + 端部关键态簇(state/duration/steps/usage/events),
// 关键态与 events 同地位(右端对齐、同为 muted font-mono)。
import { StateBadge, useLanguage } from '@gh-puller/ui';
import type { ConnStatus } from '../hooks/useMonitorSocket';
import type { SessionMeta } from '../monitor-data';

interface Props {
  status: ConnStatus;
  current: string | null;
  events: number;
  /** 当前会话元数据(state/provider/model/run_id),为空则仅显示事件计数。 */
  meta?: SessionMeta | null;
  duration?: string;
  steps?: number;
  usage?: { input: number; output: number } | null;
}

const DOT: Record<ConnStatus, string> = {
  connected: 'bg-emerald-500',
  connecting: 'bg-amber-500',
  closed: 'bg-red-500',
};

export default function MonitorStatusBar({ status, current, events, meta, duration, steps, usage }: Props) {
  const { t } = useLanguage();
  return (
    <div className="flex items-center gap-3 border-t border-[var(--border-color)] px-3 py-1.5 text-xs text-[var(--muted)]">
      <span className="flex items-center gap-1.5">
        <span className={`size-2 rounded-full ${DOT[status]}`} />
        {t(`status.${status}`)}
      </span>
      {current && <span className="truncate font-mono">{current}</span>}
      {/* 端部:会话关键态簇(右端对齐,与 events 同地位);未选中会话时不渲染任何 key/value */}
      {meta && (
        <span className="ml-auto flex min-w-0 flex-wrap items-center gap-2">
          <StateBadge state={meta.state} label={t(`session.state.${meta.state}`)} />
          {meta.provider && <span className="font-mono">{meta.provider}/{meta.model || '—'}</span>}
          <span className="font-mono">{t('meta.duration')} {duration}</span>
          {typeof steps === 'number' && steps > 0 && (
            <span className="font-mono">{t('meta.steps')} {steps}</span>
          )}
          {usage && <span className="font-mono">{usage.input}→{usage.output} tok</span>}
          <span className="font-mono">{t('statusbar.events')} {events}</span>
        </span>
      )}
    </div>
  );
}
