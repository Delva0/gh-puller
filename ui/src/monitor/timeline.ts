// 轨迹时间轴(仿 dsh ui-trajectory/src/client/timeline.ts):四种模式 +
// 范围选择 + 偏移格式化。纯函数,组件只渲染。

import type { RequestView } from './types';

export type TimelineMode = 'sequence' | 'duration' | 'time' | 'actual';

export interface TimelinePoint {
  seq: number;
  offset: number; // 0..1 比例
}

export interface TimelineModel {
  points: TimelinePoint[];
  range: { start: number; end: number }; // 全量 seq 界限
  ticks: string[]; // 起始/中点/结束标签
}

export function deriveTrajectoryTimeline(requests: RequestView[], mode: TimelineMode): TimelineModel {
  if (!requests.length) {
    return { points: [], range: { start: 0, end: 0 }, ticks: ['', '', ''] };
  }
  const minSeq = requests[0].seq;
  const maxSeq = requests[requests.length - 1].seq;
  const ts = requests.map((r) => r.ts ?? 0);
  const minTs = Math.min(...ts);
  const maxTs = Math.max(...ts, minTs + 1);
  const totalDur = Math.max(
    requests.reduce((acc, r) => acc + (r.durationMs ?? 0), 0),
    1,
  );
  const n = requests.length;
  let cum = 0;
  const points: TimelinePoint[] = requests.map((r, i) => {
    if (mode === 'sequence') return { seq: r.seq, offset: i / Math.max(1, n - 1) }; // 序号均分
    if (mode === 'actual') return { seq: r.seq, offset: (r.seq - minSeq) / Math.max(1, maxSeq - minSeq) }; // 实际 log 间距
    if (mode === 'duration') {
      cum += r.durationMs ?? 0; // 累计时长(堆叠概览)
      return { seq: r.seq, offset: cum / totalDur };
    }
    return { seq: r.seq, offset: (ts[i] - minTs) / Math.max(1, maxTs - minTs) }; // 墙上时间
  });
  const label = (i: number) => {
    if (mode === 'sequence') return `#${i + 1}`;
    if (mode === 'actual') return String(points[i].seq);
    if (mode === 'duration') return formatTimelineOffset(requests[i].durationMs ?? 0, 'duration');
    return formatTimelineOffset(ts[i], 'time');
  };
  const mid = Math.floor((n - 1) / 2);
  return { points, range: { start: minSeq, end: maxSeq }, ticks: [label(0), label(mid), label(n - 1)] };
}

/** 范围选择(seq 界限)在点集上裁剪 → 连续下标区间(含)。 */
export function trajectoryTimelineFocusIndexes(
  points: TimelinePoint[],
  range: { start: number; end: number },
): readonly [number, number] {
  const start = points.findIndex((p) => p.seq >= range.start);
  let end = -1;
  for (let i = points.length - 1; i >= 0; i--) {
    if (points[i].seq <= range.end) {
      end = i;
      break;
    }
  }
  if (start < 0 || end < 0) return [0, Math.max(0, points.length - 1)] as const;
  return [start, end] as const;
}

/** 时间/时长偏移的短格式(ms → "1.2s"/"320ms";时间戳 → HH:MM:SS)。 */
export function formatTimelineOffset(ms: number, mode: 'duration' | 'time'): string {
  if (mode === 'time') {
    const d = new Date(ms);
    const p = (v: number) => String(v).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}
