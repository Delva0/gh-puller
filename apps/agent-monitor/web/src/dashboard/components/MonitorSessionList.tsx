'use client';

// 监控侧栏会话列表:搜索/状态筛选 + run_id 分组头(任务级会话组,组头带条数)
// + 逐项右端 ··· 菜单(dsh Menu 原语,portal 免裁切):删除会话走两步 —
// 菜单(危险项)→ RiskConfirmation 弹框(勾选确认)→ onDelete(hub delete 帧)。
// 后端删除是内存+磁盘一体删,不可恢复,故确认弹框不可跳过。

import { useMemo, useState } from 'react';
import { StateBadge } from '@gh-puller/ui';
import { useLanguage } from '@gh-puller/ui';
import type { SessionMeta } from '../monitor-data';
import { IconEllipsisOutline16, Menu, RiskConfirmation } from '../vendor/dsh/ui-primitives/src/index.ts';

interface Props {
  sessions: SessionMeta[];
  current: string | null;
  onSelect: (session: string) => void;
  onDelete: (session: string) => void;
  query: string;
  stateFilter: string;
}

const fmt = (ts: number) =>
  ts > 0
    ? new Date(ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : '';

export default function MonitorSessionList({ sessions, current, onSelect, onDelete, query, stateFilter }: Props) {
  const { t } = useLanguage();
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [confirmFor, setConfirmFor] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
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

  const confirmLabel = confirmFor === null ? '' : (rows.find((x) => x.session === confirmFor)?.label ?? confirmFor);

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
          {/* 行拆两段:选择区(button)+ 菜单锚点;避免嵌套 button 的非法 DOM */}
          <div className="flex items-center pr-2">
            <button
              type="button"
              onClick={() => { setMenuFor(null); onSelect(s.session); }}
              className={`flex min-w-0 flex-1 items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-[var(--muted)]/10 ${
                current === s.session ? 'bg-[var(--accent-primary)]/10' : ''
              }`}
            >
              <span className="flex-1 truncate font-mono">{s.label}</span>
              {(s.generator || s.provider) && (
                <span className="font-mono text-[10px] text-[var(--muted)]">
                  {s.generator ? `${s.generator}·` : ''}{s.provider || '—'}/{s.model || '—'}
                </span>
              )}
              <StateBadge state={s.state} label={t(`session.state.${s.state}`)} />
              <span className="font-mono text-[10px] text-[var(--muted)]">{fmt(s.last_ts)}</span>
            </button>
            <Menu
              open={menuFor === s.session}
              portal
              align="end"
              compact
              items={[{ id: 'delete', label: t('session.delete'), danger: true }]}
              onClose={() => setMenuFor(null)}
              onSelect={(id) => {
                setMenuFor(null);
                if (id === 'delete') {
                  setAcknowledged(false);
                  setConfirmFor(s.session);
                }
              }}
              anchor={
                <button
                  type="button"
                  aria-label={t('session.actions')}
                  aria-haspopup="menu"
                  onClick={() => setMenuFor(menuFor === s.session ? null : s.session)}
                  className="flex size-7 shrink-0 items-center justify-center rounded-md border border-[var(--border-color)] text-[var(--muted)] hover:border-[var(--accent-primary)] hover:text-[var(--foreground)]"
                >
                  <IconEllipsisOutline16 size={16} />
                </button>
              }
            />
          </div>
        </li>
      ))}
      {/* 删除确认:后端删文件不可恢复,勾选后才允许确认 */}
      <RiskConfirmation
        open={confirmFor !== null}
        title={t('delete.title')}
        description={t('delete.description', { label: confirmLabel })}
        acknowledgeLabel={t('delete.acknowledge')}
        cancelLabel={t('common.cancel')}
        confirmLabel={t('session.delete')}
        acknowledged={acknowledged}
        onAcknowledgedChange={setAcknowledged}
        onCancel={() => { setAcknowledged(false); setConfirmFor(null); }}
        onConfirm={() => {
          const target = confirmFor;
          setAcknowledged(false);
          setConfirmFor(null);
          if (target !== null) onDelete(target);
        }}
      />
    </ul>
  );
}
