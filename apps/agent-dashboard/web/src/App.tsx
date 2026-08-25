// 监控面板布局:侧栏(搜索/筛选/会话列表)+ 主区(统计条 + 视图切换 + 面板)+ 状态栏
import { useEffect, useMemo, useRef, useState } from 'react';
import { StateBadge, ThemeToggle, useLanguage } from '@gh-puller/ui';
import { useMonitorSocket } from './hooks/useMonitorSocket';
import SessionList from './components/SessionList';
import StreamView from './components/StreamView';
import EventStreamView from './components/EventStreamView';
import StatusBar from './components/StatusBar';
import type { SessionState } from './types';

const CHIP = 'rounded-md border border-zinc-200 px-2 py-0.5 font-mono text-[11px] text-zinc-600 dark:border-zinc-800 dark:text-zinc-400';

export default function App() {
  const { t, lang, setLang } = useLanguage();
  const m = useMonitorSocket();

  const [query, setQuery] = useState('');
  const [stateFilter, setStateFilter] = useState<SessionState | 'all'>('all');
  const [view, setView] = useState<'stream' | 'events'>('stream');
  const [autoScroll, setAutoScroll] = useState(true);
  const [expandThinking, setExpandThinking] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const meta = useMemo(
    () => m.sessions.find((s) => s.session === m.current) ?? null,
    [m.sessions, m.current],
  );
  // 终态统计取自最后一行 session.end;运行中的实时长(k vs last_ts 即时表现)
  const summary = useMemo(() => {
    if (!m.lines.length) return null;
    const last = m.lines[m.lines.length - 1];
    return last.type === 'session.end' ? last : null;
  }, [m.lines]);

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [view, autoScroll, m.lines.length, m.events.length]);

  const duration = summary?.duration_ms != null
    ? summary.duration_ms > 1000
      ? `${(summary.duration_ms / 1000).toFixed(1)}s`
      : `${summary.duration_ms}ms`
    : meta && meta.state === 'running'
      ? `${Math.max(0, meta.last_ts - meta.ts).toFixed(0)}s`
      : '—';
  const usage = (summary?.usage ?? null) as { input_tokens?: number | null; output_tokens?: number | null } | null;

  return (
    <div className="flex h-full bg-zinc-100 text-zinc-800 dark:bg-zinc-950 dark:text-zinc-200">
      {/* 侧栏 */}
      <aside className="flex w-72 shrink-0 flex-col border-r border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex items-center justify-between border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
          <h1 className="text-sm font-semibold">{t('app.title')}</h1>
          <div className="flex items-center gap-1.5">
            <ThemeToggle />
            <button
              type="button"
              onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}
              className="rounded-md border border-zinc-200 px-2 py-1 text-[11px] text-zinc-500 hover:border-zinc-400 dark:border-zinc-700 dark:text-zinc-400"
            >
              {t('toolbar.lang')}
            </button>
          </div>
        </div>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('sidebar.search')}
          className="mx-3 mt-3 rounded-md border border-zinc-200 bg-transparent px-2 py-1.5 text-xs placeholder:text-zinc-400 focus:border-emerald-500 focus:outline-none dark:border-zinc-700"
        />
        <select
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value as SessionState | 'all')}
          className="mx-3 mt-2 rounded-md border border-zinc-200 bg-transparent px-2 py-1 text-xs text-zinc-500 dark:border-zinc-700 dark:text-zinc-400"
        >
          <option value="all">{t('sidebar.filter.all')}</option>
          <option value="running">{t('session.state.running')}</option>
          <option value="completed">{t('session.state.completed')}</option>
          <option value="aborted">{t('session.state.aborted')}</option>
        </select>
        <div className="mt-2 flex-1 overflow-y-auto">
          <SessionList sessions={m.sessions} current={m.current} query={query} stateFilter={stateFilter} onSelect={m.select} />
        </div>
      </aside>

      {/* 主区 */}
      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-zinc-200 px-4 py-2 dark:border-zinc-800">
          <div className="flex overflow-hidden rounded-md border border-zinc-200 text-xs dark:border-zinc-700">
            {(['stream', 'events'] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-3 py-1 ${view === v
                  ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                  : 'text-zinc-500 hover:text-zinc-300'}`}
              >
                {t(v === 'stream' ? 'view.stream' : 'view.events')}
              </button>
            ))}
          </div>
          <label className="flex cursor-pointer items-center gap-1 text-xs text-zinc-500">
            <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
            {t('toolbar.autoScroll')}
          </label>
          {view === 'stream' && (
            <button
              onClick={() => setExpandThinking((v) => !v)}
              className="rounded-md border border-zinc-200 px-2 py-1 text-xs text-zinc-500 hover:border-zinc-400 dark:border-zinc-700 dark:text-zinc-400"
            >
              {expandThinking ? t('toolbar.collapseThinking') : t('toolbar.expandThinking')}
            </button>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            {meta && (
              <>
                <StateBadge state={meta.state} label={t(`session.state.${meta.state}`)} />
                {meta.provider && <span className={CHIP}>{meta.provider}/{meta.model || '—'}</span>}
                <span className={CHIP}>{t('meta.duration')} {duration}</span>
                {summary?.num_rounds != null && <span className={CHIP}>{t('meta.rounds')} {summary.num_rounds}</span>}
                {summary?.text_chars != null && <span className={CHIP}>{t('meta.chars')} {summary.text_chars}</span>}
                {usage && (
                  <span className={CHIP}>
                    {usage.input_tokens ?? '?'}→{usage.output_tokens ?? '?'}
                    {usage.input_tokens != null || usage.output_tokens != null ? ' tok' : ''}
                  </span>
                )}
              </>
            )}
          </div>
        </header>

        <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-2">
          {m.current === null ? (
            <div className="p-6 text-xs text-zinc-500 dark:text-zinc-600">
              {t(view === 'stream' ? 'stream.empty' : 'events.empty')}
            </div>
          ) : view === 'stream' ? (
            <StreamView lines={m.lines} expandThinking={expandThinking} />
          ) : (
            <EventStreamView events={m.events} />
          )}
        </div>

        <StatusBar status={m.status} current={m.current} lines={m.lines.length} events={m.events.length} />
      </main>
    </div>
  );
}
