// 底部状态栏:连接状态 + 当前会话 + 行/事件计数
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

export default function StatusBar({ status, current, events }: Props) {
  const { t } = useLanguage();
  return (
    <div className="flex items-center gap-3 border-t border-zinc-200 px-3 py-1.5 text-xs text-zinc-500 dark:border-zinc-800 dark:text-zinc-500">
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
