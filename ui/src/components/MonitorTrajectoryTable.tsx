'use client';

// 轨迹表(仿 dsh TrajectoryTable:turn/step 分组行 + 单元格)。
// gh-puller 单 run 恒 1 turn,行 = step(一次 LLM 请求);单元格 = 请求(文本+usage+
// 思考)/工具/上下文/系统快照;搜索命中高亮,折叠组点击展开。

import { useState } from 'react';
import Markdown from './Markdown';
import { useLanguage } from '../contexts/LanguageContext';
import type { LayoutGroup } from '../monitor';

interface Props {
  groups: LayoutGroup[];
  hits: Set<number> | null; // 搜索命中 seq 集;null = 无搜索
  collapsed: Set<string>;
  onToggle: (key: string) => void;
}

export default function MonitorTrajectoryTable({ groups, hits, collapsed, onToggle }: Props) {
  const { t } = useLanguage();
  if (!groups.length) {
    return <div className="p-6 text-xs text-[var(--muted)]">{t('traj.empty')}</div>;
  }
  return (
    <div className="space-y-2 px-4 pb-4">
      {groups.map((g) => {
        const isCollapsed = collapsed.has(g.key);
        const cells = hits ? g.cells.filter((c) => hits.has(c.seq)) : g.cells;
        return (
          <div key={g.key} className="overflow-hidden rounded-md border border-[var(--border-color)]">
            <button
              type="button"
              onClick={() => onToggle(g.key)}
              className="flex w-full items-center gap-2 border-b border-[var(--border-color)] bg-[var(--card-bg)] px-3 py-1.5 text-left"
            >
              <span className="w-1 shrink-0 text-[var(--muted)]">{isCollapsed ? '▸' : '▾'}</span>
              <span className="font-mono text-xs font-semibold text-[var(--foreground)]">
                {t('traj.groupLabel', { step: g.step })}
              </span>
              {g.cells.some((c) => c.kind === 'request') && (
                <UsageChip group={g} />
              )}
              <span className="ml-auto font-mono text-[10px] text-[var(--muted)]">{g.cells.length}</span>
            </button>
            {!isCollapsed && (
              <div className="divide-y divide-[var(--border-color)] bg-[var(--background)]">
                {cells.map((c) => (
                  <CellRow key={c.key} cell={c} hit={Boolean(hits?.has(c.seq))} />
                ))}
                {cells.length === 0 && (
                  <div className="px-3 py-2 text-xs text-[var(--muted)]">{t('traj.noHit')}</div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function UsageChip({ group }: { group: LayoutGroup }) {
  const req = group.cells.find((c) => c.kind === 'request')?.request;
  if (!req) return null;
  return (
    <span className="font-mono text-[10px] text-[var(--muted)]">
      {req.usage?.input_tokens ?? '?'}→{req.usage?.output_tokens ?? '?'} tok
      {req.durationMs != null && ` · ${req.durationMs}ms`}
    </span>
  );
}

function CellRow({ cell, hit }: { cell: LayoutGroup['cells'][number]; hit: boolean }) {
  const { t } = useLanguage();
  const ring = hit ? 'ring-1 ring-[var(--accent-primary)]' : '';
  if (cell.kind === 'request' && cell.request) {
    const r = cell.request;
    return (
      <div className={`px-3 py-2 ${ring}`}>
        {r.thinking && <div className="mb-1 font-mono text-[10px] text-[var(--muted)]">🧠 {r.thinking.slice(0, 160)}</div>}
        <div className="text-[13px]">
          <Markdown content={r.text} />
        </div>
        {r.error && <div className="mt-1 font-mono text-[11px] text-red-500">{r.error}</div>}
        {r.interrupted && <div className="mt-1 font-mono text-[10px] text-amber-500">{t('traj.interrupted')}</div>}
        {r.retry && <div className="mt-1 font-mono text-[10px] text-amber-500">{t('view.retryLabel', { attempt: r.retry.attempt })}</div>}
      </div>
    );
  }
  if (cell.kind === 'tool' && cell.tool) {
    return (
      <div className={`px-3 py-1.5 font-mono text-[11px] ${ring}`}>
        <span className="text-[var(--accent-primary)]">🔧 {cell.tool.name}</span>
        <span className="ml-2 text-[var(--muted)]">{cell.tool.callId}</span>
        {cell.tool.isError && <span className="ml-2 text-red-500">{t('view.errorLabel')}</span>}
      </div>
    );
  }
  if (cell.kind === 'context') {
    return (
      <div className={`px-3 py-1.5 text-[11px] text-[var(--muted)] ${ring}`}>
        <span className="mr-1">⤷</span>
        {(cell.text ?? '').slice(0, 200)}
      </div>
    );
  }
  if (cell.kind === 'system') {
    return (
      <div className={`px-3 py-1.5 font-mono text-[11px] text-[var(--muted)] ${ring}`}>{cell.text}</div>
    );
  }
  return null;
}
