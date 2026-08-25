// 监控面板布局:侧栏(搜索/筛选/run_id 分组会话列表)+ 主面板(状态芯片条 +
// 对话/轨迹 双 Tab,组件取自 @gh-puller/ui)+ 状态栏
import { useEffect, useMemo, useState } from 'react';
import {
  MonitorChatView,
  MonitorConversation,
  MonitorSessionList,
  MonitorStatusBar,
  MonitorTrajectoryView,
  StateBadge,
  ThemeToggle,
  useLanguage,
  useMonitorSession,
  useMonitorSocket,
} from '@gh-puller/ui';
import type { MonitorView, ToolCallView } from '@gh-puller/ui';

const CHIP = 'rounded-md border border-[var(--border-color)] px-2 py-0.5 font-mono text-[11px] text-[var(--muted)]';

export default function App() {
  const { t, lang, setLang } = useLanguage();
  const m = useMonitorSocket();
  const { events, chat, partial } = useMonitorSession();

  const [query, setQuery] = useState('');
  const [stateFilter, setStateFilter] = useState<string>('all');
  const [view, setView] = useState<MonitorView>('chat');
  const [autoScroll, setAutoScroll] = useState(true);

  const meta = useMemo(() => m.sessions.find((s) => s.session === m.current) ?? null, [m.sessions, m.current]);
  // 终态摘要取自 session/end;运行中时长按 last_ts - ts 即时表现
  const summary = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].type === 'session/end') return events[i].data as Record<string, unknown>;
    }
    return null;
  }, [events]);
  const usage = useMemo(() => {
    // 汇总各请求 usage(会话级摘要)
    let inp: number | null = null;
    let out: number | null = null;
    for (const r of chat.requests) {
      if (r.usage?.input_tokens != null) inp = (inp ?? 0) + r.usage.input_tokens;
      if (r.usage?.output_tokens != null) out = (out ?? 0) + r.usage.output_tokens;
    }
    return inp == null && out == null ? null : { input_tokens: inp, output_tokens: out };
  }, [chat.requests]);
  const toolsByCall = useMemo(() => {
    const map = new Map<string, ToolCallView>();
    for (const r of chat.requests) {
      for (const tool of r.tools) map.set(tool.callId, tool);
    }
    return map;
  }, [chat.requests]);

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
              {chat.requests.length > 0 && (
                <span className={CHIP}>{t('meta.steps')} {chat.requests.length}</span>
              )}
              {usage && (
                <span className={CHIP}>
                  {usage.input_tokens ?? '?'}→{usage.output_tokens ?? '?'}
                  {usage.input_tokens != null || usage.output_tokens != null ? ' tok' : ''}
                </span>
              )}
            </>
          )}
          <label className="ml-auto flex cursor-pointer items-center gap-1 text-xs text-[var(--muted)]">
            <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
            {t('monitor.autoScroll')}
          </label>
        </header>

        <MonitorConversation view={view} onView={setView}>
          {m.current === null ? (
            <div className="p-6 text-xs text-[var(--muted)]">{t('view.empty')}</div>
          ) : view === 'chat' ? (
            <MonitorChatView nodes={chat.chatNodes} partial={partial} toolsByCall={toolsByCall} autoScroll={autoScroll} />
          ) : (
            <MonitorTrajectoryView requests={chat.requests} nodes={chat.chatNodes} />
          )}
        </MonitorConversation>

        <MonitorStatusBar status={m.status} current={m.current} events={events.length} />
      </main>
    </div>
  );
}
