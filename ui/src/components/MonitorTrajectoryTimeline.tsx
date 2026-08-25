'use client';

// 轨迹时间轴条:四种模式的点位分布 + 点击拖动选择范围(点击起止两点)
// (仿 dsh TrajectoryTimeline 的 range selection;实现简化:两次点击定区间)

import { useState } from 'react';
import { useLanguage } from '../contexts/LanguageContext';
import type { TimelineModel } from '../monitor';

interface Props {
  model: TimelineModel;
  range: { start: number; end: number };
  onRange: (r: { start: number; end: number }) => void;
}

export default function MonitorTrajectoryTimeline({ model, range, onRange }: Props) {
  const { t } = useLanguage();
  const [anchor, setAnchor] = useState<number | null>(null);
  const active = range.start !== model.range.start || range.end !== model.range.end;

  const pick = (seq: number) => {
    if (anchor === null) {
      setAnchor(seq);
      return;
    }
    if (seq === anchor) {
      setAnchor(null);
      onRange({ start: seq, end: seq });
      return;
    }
    onRange({ start: Math.min(anchor, seq), end: Math.max(anchor, seq) });
    setAnchor(null);
  };

  return (
    <div className="px-4">
      <div className="relative h-9">
        {/* 选中区间带 */}
        {active && (
          <div
            className="absolute inset-y-0 rounded bg-[var(--accent-primary)]/10"
            style={bandStyle(model, range)}
          />
        )}
        {model.points.map((p) => (
          <button
            key={p.seq}
            type="button"
            title={t('traj.pointTitle', { seq: p.seq })}
            onClick={() => pick(p.seq)}
            className={`absolute inset-y-1.5 size-3 translate-x-[-50%] rounded-full transition-colors ${
              anchor === p.seq || (active && p.seq >= range.start && p.seq <= range.end)
                ? 'bg-[var(--accent-primary)]'
                : 'bg-[var(--accent-secondary)] hover:bg-[var(--accent-primary)]'
            }`}
            style={{ left: `${(p.offset * 100).toFixed(2)}%` }}
          />
        ))}
        {/* 刻度标签 */}
        {model.ticks.map((label, i) => (
          <span
            key={i}
            className="absolute -bottom-0.5 translate-x-[-50%] font-mono text-[9px] text-[var(--muted)]"
            style={{ left: `${(i / 2) * 100}%` }}
          >
            {label}
          </span>
        ))}
      </div>
      <div className="flex items-center gap-2 pb-1 text-[10px] text-[var(--muted)]">
        <span>{t('traj.rangeHint')}</span>
        {active && (
          <button
            type="button"
            onClick={() => {
              setAnchor(null);
              onRange({ start: model.range.start, end: model.range.end });
            }}
            className="text-[var(--accent-primary)] hover:underline"
          >
            {t('traj.resetRange')}
          </button>
        )}
      </div>
    </div>
  );
}

function bandStyle(model: TimelineModel, range: { start: number; end: number }): { left: string; right: string } {
  const sorted = [...model.points].sort((a, b) => a.offset - b.offset);
  const first = sorted.find((p) => p.seq >= range.start);
  const last = sorted.find((p) => p.seq <= range.end);
  const left = first ? first.offset : 0;
  const right = last ? 1 - last.offset : 1;
  return { left: `${left * 100}%`, right: `${Math.max(0, right) * 100}%` };
}
