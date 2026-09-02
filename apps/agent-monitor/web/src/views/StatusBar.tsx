'use client';

/** Render connection state and canonical run counters. */
import { StateBadge, useLanguage } from '@gh-puller/ui';
import type { SessionMeta } from '../monitor/protocol';
import type { ConnStatus } from '../monitor/useMonitorSocket';

interface Props {
  status: ConnStatus;
  current: string | null;
  events: number;
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

export default function StatusBar({ status, current, events, meta, duration, steps, usage }: Props) {
  const { t } = useLanguage();
  return (
    <div className="flex items-center gap-3 border-t border-[var(--border-color)] px-3 py-1.5 text-xs text-[var(--muted)]">
      <span className="flex items-center gap-1.5">
        <span className={`size-2 rounded-full ${DOT[status]}`} />
        {t(`status.${status}`)}
      </span>
      {current && <span className="truncate font-mono">{current}</span>}
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
