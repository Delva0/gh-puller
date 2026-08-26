// 监控面板布局:侧栏(搜索/筛选/run_id 分组会话列表)+ 主面板(状态芯片条 +
// dsh 1:1 对话/轨迹面板(DshConversationPanel,组件取自 @gh-puller/ui))+ 状态栏
import { useMemo, useState } from 'react';
import { StateBadge, ThemeToggle, useLanguage } from '@gh-puller/ui';
import {
  DshConversationPanel,
  MonitorSessionList,
  MonitorStatusBar,
  useMonitorSession,
  useMonitorSocket,
} from './dashboard';

const CHIP = 'rounded-md border border-[var(--border-color)] px-2 py-0.5 font-mono text-[11px] text-[var(--muted)]';

/** 从 dsh 轨迹快照汇总 tok(usage 字段以 dsh 侧形状为准则)。 */
function usageOf(requests: ReadonlyArray<{ usage?: unknown }>): { input: number; output: number } | null {
  let inp = 0;
  let out = 0;
  let seen = false;
  for (const r of requests) {
    const u = r.usage as { input?: number; output?: number; inputTokens?: number; outputTokens?: number } | null | undefined;
    if (u == null) continue;
    const i = u.input ?? u.inputTokens;
    const o = u.output ?? u.outputTokens;
    if (i != null) { inp += i; seen = true; }
    if (o != null) { out += o; seen = true; }
  }
  return seen ? { input: inp, output: out } : null;
}

export default function App() {
  const { t, lang, setLang } = useLanguage();
  const m = useMonitorSocket();
  const { events, dsh } = useMonitorSession();

  const [query, setQuery] = useState('');
  const [stateFilter, setStateFilter] = useState<string>('all');

  const conv = dsh?.snapshot();
  const traj = conv?.views.get('trajectory');
  const requests = traj?.requests ?? [];

  const meta = useMemo(() => m.sessions.find((s) => s.session === m.current) ?? null, [m.sessions, m.current]);
  // 终态摘要取自 session/end;运行中时长按 last_ts - ts 即时表现
  const summary = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].type === 'session/end') return events[i].data as Record<string, unknown>;
    }
    return null;
  }, [events]);
  const usage = useMemo(() => usageOf(requests), [requests]);

  const durMs = Number(summary?.duration_ms);
  const duration = Number.isFinite(durMs) && summary && summary.duration_ms != null
    ? durMs > 1000
      ? `${(durMs / 1000).toFixed(1)}s`
      : `${durMs}ms`
    : meta && meta.state === 'running'
      ? `${Math.max(0, meta.last_ts - meta.ts).toFixed(0)}s`
      : '—';

  return (
    <div className="flex h-full bg-[var(--background)] text-[var(--foreground)]">
      {/* 侧栏 */}
      <aside className="flex w-72 shrink-0 flex-col border-r border-[var(--border-color)] bg-[var(--card-bg)]">
        <div className="flex items-center justify-between border-b border-[var(--border-color)] px-3 py-2">
          <h1 className="text-sm font-semibold">{t('app.title')}</h1>
          <div className="flex items-center gap-1.5">
            <ThemeToggle />
            <button
              type="button"
              onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}
              className="rounded-md border border-[var(--border-color)] px-2 py-1 text-[11px] text-[var(--muted)] hover:border-[var(--accent-secondary)]"
            >
              {t('toolbar.lang')}
            </button>
          </div>
        </div>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('sidebar.search')}
          className="mx-3 mt-3 rounded-md border border-[var(--border-color)] bg-transparent px-2 py-1.5 text-xs placeholder:text-[var(--muted)] focus:border-[var(--accent-primary)] focus:outline-none"
        />
        <select
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value)}
          className="mx-3 mt-2 rounded-md border border-[var(--border-color)] bg-transparent px-2 py-1 text-xs text-[var(--muted)]"
        >
          <option value="all">{t('sidebar.filter.all')}</option>
          <option value="running">{t('session.state.running')}</option>
          <option value="completed">{t('session.state.completed')}</option>
          <option value="aborted">{t('session.state.aborted')}</option>
        </select>
        <div className="mt-2 flex-1 overflow-y-auto">
          <MonitorSessionList
            sessions={m.sessions}
            current={m.current}
            query={query}
            stateFilter={stateFilter}
            onSelect={m.select}
          />
        </div>
      </aside>

      {/* 主区 */}
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-2 border-b border-[var(--border-color)] px-4 py-2">
          {meta && (
            <>
              <StateBadge state={meta.state} label={t(`session.state.${meta.state}`)} />
              {meta.provider && <span className={CHIP}>{meta.provider}/{meta.model || '—'}</span>}
              {meta.run_id && <span className={CHIP}>{meta.run_id}</span>}
              <span className={CHIP}>{t('meta.duration')} {duration}</span>
              {requests.length > 0 && (
                <span className={CHIP}>{t('meta.steps')} {requests.length}</span>
              )}
              {usage && (
                <span className={CHIP}>
                  {usage.input}→{usage.output} tok
                </span>
              )}
            </>
          )}
        </header>

        <div className="min-h-0 flex-1">
          {m.current === null ? (
            <div className="p-6 text-xs text-[var(--muted)]">{t('view.empty')}</div>
          ) : (
            <DshConversationPanel locale={lang} />
          )}
        </div>

        <MonitorStatusBar status={m.status} current={m.current} events={events.length} />
      </main>
    </div>
  );
}
