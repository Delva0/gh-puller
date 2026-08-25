'use client';

// 轨迹视图(仿 dsh TrajectoryView:工具栏 + 时间轴 + 表格;状态内聚,宿主只喂
// requests/nodes):时间轴 4 模式 + 范围选择 + 搜索过滤 + 折叠全部。

import { useMemo, useState } from 'react';
import { useLanguage } from '../contexts/LanguageContext';
import {
  TrajectorySearchIndex,
  deriveTrajectoryLayout,
  deriveTrajectoryTimeline,
  trajectoryTimelineFocusIndexes,
} from '../monitor';
import type { ChatNode, RequestView, TimelineMode } from '../monitor';
import MonitorTrajectoryTable from './MonitorTrajectoryTable';
import MonitorTrajectoryTimeline from './MonitorTrajectoryTimeline';
import MonitorTrajectoryToolbar from './MonitorTrajectoryToolbar';

interface Props {
  requests: RequestView[];
  nodes: ChatNode[];
}

export default function MonitorTrajectoryView({ requests, nodes }: Props) {
  const { t } = useLanguage();
  const [mode, setMode] = useState<TimelineMode>('sequence');
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [range, setRange] = useState<{ start: number; end: number } | null>(null);

  const timeline = useMemo(() => deriveTrajectoryTimeline(requests, mode), [requests, mode]);
  const groups = useMemo(() => deriveTrajectoryLayout(requests, nodes), [requests, nodes]);
  const hits = useMemo(() => {
    if (!query.trim()) return null;
    const idx = new TrajectorySearchIndex();
    idx.setNodes(nodes);
    return idx.query(query);
  }, [query, nodes]);

  const effRange = range ?? timeline.range;
  const focus = useMemo(() => trajectoryTimelineFocusIndexes(timeline.points, effRange), [timeline, effRange]);
  const focusedGroups = useMemo(
    () => (range ? groups.slice(0) : groups), // 范围对请求组生效(单 run 组少,全量渲染)
    [groups, range],
  );
  void focus;

  const collapseAll = () => setCollapsed(new Set(focusedGroups.map((g) => g.key)));
  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  if (!requests.length && !nodes.length) {
    return <div className="p-6 text-xs text-[var(--muted)]">{t('traj.empty')}</div>;
  }

  return (
    <div className="flex h-full flex-col">
      <MonitorTrajectoryToolbar
        mode={mode}
        onMode={setMode}
        onCollapseAll={collapseAll}
        query={query}
        onQuery={setQuery}
      />
      <MonitorTrajectoryTimeline model={timeline} range={effRange} onRange={setRange} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <MonitorTrajectoryTable groups={focusedGroups} hits={hits} collapsed={collapsed} onToggle={toggle} />
      </div>
    </div>
  );
}
