// 监控面板布局:侧栏(可拖拽伸缩/搜索/筛选/run_id 分组会话列表)+ 主面板(dsh 1:1
// 对话/轨迹面板(DshConversationPanel,组件取自 @gh-puller/ui))+ 状态栏(端部关键态簇)
import { useMemo, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { ThemeToggle, useLanguage } from '@gh-puller/ui';
import {
  DshConversationPanel,
  MonitorSessionList,
  MonitorStatusBar,
  useMonitorSession,
  useMonitorSocket,
} from './dashboard';

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
  // 侧栏可拖拽伸缩(边缘手柄),w-72=288px 为默认
  const [sideWidth, setSideWidth] = useState(288);

  const startSideResize = (e: ReactPointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = sideWidth;
    const move = (ev: PointerEvent) => {
      setSideWidth(Math.max(210, Math.min(520, startW + (ev.clientX - startX))));
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  };

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
      <aside
        className="flex shrink-0 flex-col border-r border-[var(--border-color)] bg-[var(--card-bg)]"
        style={{ width: sideWidth }}
      >
        <div className="flex items-center justify-between border-b border-[var(--border-color)] px-3 py-2">
          <h1 className="text-sm font-semibold">{t('app.title')}</h1>
          <div className="flex items-center gap-1.5">
            <ThemeToggle />
            <button
              type="button"
              onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}
              className="flex h-7 w-7 items-center justify-center rounded-md border border-[var(--border-color)] text-[11px] leading-none text-[var(--muted)] hover:border-[var(--accent-secondary)]"
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
            onDelete={m.remove}
          />
        </div>
      </aside>

      {/* 侧栏拖拽伸缩手柄 */}
      <div
        onPointerDown={startSideResize}
        aria-hidden="true"
        className="w-1.5 shrink-0 cursor-col-resize select-none touch-none bg-transparent transition-colors hover:bg-[var(--accent-primary)]/25 active:bg-[var(--accent-primary)]/35"
      />

      {/* 主区 */}
      <main className="flex min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1">
          {m.current === null ? (
            <div className="p-6 text-xs text-[var(--muted)]">{t('view.empty')}</div>
          ) : (
            <DshConversationPanel locale={lang} />
          )}
        </div>

        <MonitorStatusBar
          status={m.status}
          current={m.current}
          events={events.length}
          meta={meta}
          duration={duration}
          steps={requests.length}
          usage={usage}
        />
      </main>
    </div>
  );
}
