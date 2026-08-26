'use client';

// 底部状态栏:连接状态 + 当前会话 + 行/事件计数(监控宿主主面板组装用)
import { useLanguage } from '@gh-puller/ui';
import type { ConnStatus } from '../hooks/useMonitorSocket';

interface Props {
  status: ConnStatus;
  current: string | null;
  events: number;
}

const DOT: Record<ConnStatus, string> = {
  connected: 'bg-emerald-500',
  connecting: 'bg-amber-500',
  closed: 'bg-red-500',
};

export default function MonitorStatusBar({ status, current, events }: Props) {
  const { t } = useLanguage();
  return (
    <div className="flex items-center gap-3 border-t border-[var(--border-color)] px-3 py-1.5 text-xs text-[var(--muted)]">
      <span className="flex items-center gap-1.5">
        <span className={`size-2 rounded-full ${DOT[status]}`} />
        {t(`status.${status}`)}
      </span>
      {current && <span className="truncate font-mono">{current}</span>}
      <span className="ml-auto font-mono">
        {t('statusbar.events')} {events}
      </span>
    </div>
  );
}
