import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { ArrowDown, ArrowUp, Check, ChevronDown, CircleAlert, Download, History, LoaderCircle,
  LogOut, Menu, MessageSquarePlus, Moon, PanelLeftClose, Search, Settings2, Square, Sun, Trash2, Upload, X, Pencil } from 'lucide-react';
import { api, ApiError } from './api';
import { Mark, Modal, TurnView, turnsFrom } from './components';
import { SettingsPanel } from './SettingsPanel';
import { CompactSelect, PasswordInput } from './controls';
import { LanguageContext, translator } from './i18n';
import { useModels } from './useModels';
import { useValidation } from './useValidation';
import { availabilityHint } from './ui-model';
import { fork, selectBranch, versions } from './branches';
import { deleteChat, exportEvents, download, exportHistory, importHistory, loadHistory, loadSettings, saveChat, saveSettings } from './db';
import { emptyCredentials, emptyPreferences, eventSchema, isRunning, publicData, settingsFor,
  type Agent, type Catalog, type ChatEvent, type Conversation, type Credentials, type ModelSettings,
  type Preferences, type SessionView, type Settings } from './types';

type Pending = { request_id: string; prompt: string; settings: Settings; credentials: Credentials; server_id?: string };
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试';

export default function App() {
  const [authenticated, setAuthenticated] = useState(false);
  const [checking, setChecking] = useState(true);
  const [password, setPassword] = useState('');
  const [loginError, setLoginError] = useState('');
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [chats, setChats] = useState<Conversation[]>([]);
  const chatsRef = useRef(chats);
  const [activeId, setActiveId] = useState('');
  const [preferences, setPreferences] = useState<Preferences>(() => ({ ...emptyPreferences, language: localStorage.getItem('trace-language') === 'en' ? 'en' : 'zh' }));
  const t = translator(preferences.language);
  const [theme, setTheme] = useState(() => localStorage.getItem('trace-theme') === 'light' ? 'light' : 'dark');
  const [credentials, setCredentials] = useState<Credentials>(emptyCredentials);
  const keysRef = useRef(credentials);
  const [remembered, setRemembered] = useState<Record<string, string[]>>({});
  const [configDrafts, setConfigDrafts] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState('');
  const [sidebar, setSidebar] = useState(() => window.innerWidth > 760);
  const [resizing, setResizing] = useState(false);
  const [removing, setRemoving] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const [showSettings, setShowSettings] = useState(false);
  const [draft, setDraft] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const pending = useRef(new Map<string, Pending>());
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [connection, setConnection] = useState('ready');
  const [rename, setRename] = useState<Conversation | null>(null);
  const [renameText, setRenameText] = useState('');
  const [remove, setRemove] = useState<Conversation | null>(null);
  const [clock, setClock] = useState(Date.now());
  const [atBottom, setAtBottom] = useState(true);
  const follow = useRef(true);
  const lastScrollTop = useRef(0);
  const touchY = useRef<number | null>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const active = chats.find(chat => chat.id === activeId);
  const selectedAgent = active?.agent ?? preferences.agent ?? catalog?.default_agent ?? '';
  const capability = catalog?.agents.find(item => item.id === selectedAgent);
  const composerSettings = active?.settings ?? (catalog ? settingsFor(selectedAgent, preferences, catalog) : undefined);
  const turns = useMemo(() => turnsFrom(active?.events ?? []), [active?.events]);
  const running = active ? isRunning(active) : false;
  const credentialSource = active?.server_id ?? active?.source_id;
  const configured = new Set([...Object.keys(credentials).filter(key => credentials[key].trim()),
    ...(credentialSource ? remembered[credentialSource] ?? [] : [])]);
  const validation = useValidation(catalog, preferences, credentials, credentialSource, authenticated, configDrafts);
  const available = validation.agent(selectedAgent);
  const discovery = useModels(preferences.connection.base_url ?? catalog?.defaults.base_url ?? '', credentials.api_key ?? '',
    credentialSource && remembered[credentialSource]?.includes('api_key') ? credentialSource : undefined, authenticated);
  useEffect(() => {
    document.documentElement.lang = preferences.language === 'en' ? 'en' : 'zh-CN';
    localStorage.setItem('trace-language', preferences.language);
  }, [preferences.language]);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('trace-theme', theme);
  }, [theme]);
  const visibleChats = useMemo(() => [...chats].sort((a, b) => b.created.localeCompare(a.created)).filter(chat => {
    const query = search.toLocaleLowerCase();
    return chat.title.toLocaleLowerCase().includes(query) || chat.events.some(e =>
      e.type === 'query/start' && String(e.data.prompt ?? '').toLocaleLowerCase().includes(query));
  }), [chats, search]);

  const commit = useCallback((next: Conversation[], persist = true) => {
    const previous = chatsRef.current;
    chatsRef.current = next;
    setChats(next);
    if (persist) for (const chat of next) if (previous.find(c => c.id === chat.id) !== chat) {
      void saveChat(chat).catch(() => setNotice('浏览器历史保存失败。请导出记录，检查可用存储空间。'));
    }
  }, []);
  const update = useCallback((id: string, change: (chat: Conversation) => Conversation) => {
    commit(chatsRef.current.map(chat => chat.id === id ? change(chat) : chat));
  }, [commit]);

  function pauseFollow() {
    if (viewport.current && viewport.current.scrollHeight > viewport.current.clientHeight) {
      lastScrollTop.current = viewport.current.scrollTop;
      follow.current = false; setAtBottom(false);
    }
  }

  function newChat() {
    setActiveId(''); setDraft(''); setNotice(''); setConnection('ready');
    follow.current = true; setAtBottom(true);
    if (window.innerWidth <= 760) setSidebar(false);
    setTimeout(() => input.current?.focus(), 0);
  }

  async function connect() {
    const [info, sessions] = await Promise.all([
      api<Catalog>('/catalog'), api<SessionView[]>('/sessions'),
    ]);
    setCatalog(info);
    const config = await loadSettings(info).catch(() => undefined) ?? emptyPreferences;
    setPreferences(config);
    setRemembered(Object.fromEntries(sessions.map(session => [session.id, session.configured_credentials])));
    commit(chatsRef.current.map(chat => {
      const session = sessions.find(item => item.id === chat.server_id);
      return { ...chat, readonly: false, live: session?.running ?? false, server_id: session?.id,
        settings: session?.running ? chat.settings : settingsFor(chat.agent, config, info) };
    }));
    setAuthenticated(true); setLoginError(''); setConnection('ready');
    if (!chatsRef.current.length) newChat();
    setStreamEpoch(n => n + 1);
  }

  useEffect(() => {
    let disposed = false;
    async function initialize() {
      try {
        const history = await loadHistory();
        if (disposed) return;
        commit(history, false);
        setActiveId([...history].sort((a, b) => b.created.localeCompare(a.created))[0]?.id ?? '');
      } catch { setNotice('浏览器历史暂时无法读取，可以继续聊天并导出记录。'); }
      try {
        await api('/auth/me');
        if (!disposed) await connect();
      } catch (error) {
        if (!disposed && (!(error instanceof ApiError) || error.status !== 401)) setLoginError(errorMessage(error));
      } finally { if (!disposed) setChecking(false); }
    }
    void initialize();
    return () => { disposed = true; };
    // Bootstrap once; later connections preserve current browser state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!authenticated || !active?.server_id) return;
    const id = active.id;
    const serverId = active.server_id;
    const branchId = active.branch_id;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let checkingStatus = false;
    let queue: ChatEvent[] = [];
    const source = new EventSource(`/api/sessions/${serverId}/events?after=${active.events.at(-1)?.seq ?? 0}`);
    setConnection('connecting');
    const flush = () => {
      if (timer) clearTimeout(timer);
      timer = undefined;
      if (!queue.length) return;
      const batch = queue; queue = [];
      update(id, chat => {
        if (chat.server_id !== serverId || chat.branch_id !== branchId) return chat;
        const after = chat.events.at(-1)?.seq ?? 0;
        const seen = new Set<number>();
        const entries = batch.filter(e => e.seq > after && !seen.has(e.seq) && seen.add(e.seq)).sort((a, b) => a.seq - b.seq);
        const first = entries.find(e => e.type === 'query/start');
        const last = entries.filter(e => e.type === 'query/start' || e.type === 'query/end').at(-1);
        return { ...chat, events: [...chat.events, ...entries], live: last ? last.type === 'query/start' : chat.live,
          title: !chat.renamed && !chat.events.length && first ? String(first.data.prompt).replace(/\s+/g, ' ').slice(0, 36) : chat.title };
      });
      for (const event of batch) if (event.type === 'query/start' && pending.current.get(id)?.request_id === event.query_id) {
        pending.current.delete(id); setDraft('');
      }
    };
    source.onopen = () => { if (!disposed) setConnection('ready'); };
    source.onmessage = message => {
      try {
        const event = eventSchema.parse(JSON.parse(message.data));
        queue.push(event);
        if (event.type === 'query/end') flush();
        else timer ??= setTimeout(flush, 70);
      } catch { setNotice('收到无法识别的事件，请导出记录并重连。'); }
    };
    source.addEventListener('idle', () => {
      flush(); source.close(); setConnection('ready');
      void api<SessionView>(`/sessions/${serverId}`).then(session => {
        if (!disposed) {
          setRemembered(value => ({ ...value, [serverId]: session.configured_credentials }));
          update(id, chat => chat.server_id === serverId && chat.branch_id === branchId ? { ...chat, live: session.running } : chat);
        }
      }).catch(() => {});
    });
    source.addEventListener('expired', () => { flush(); source.close(); update(id, chat => chat.server_id === serverId ? { ...chat, live: false, server_id: undefined } : chat); });
    source.onerror = () => {
      if (disposed) return;
      setConnection('reconnecting');
      if (checkingStatus) return;
      checkingStatus = true;
      void api<SessionView>(`/sessions/${serverId}`).catch(error => {
        if (disposed) return;
        if (error instanceof ApiError && error.status === 404) {
          flush(); source.close(); update(id, chat => chat.server_id === serverId ? { ...chat, live: false, server_id: undefined } : chat); setConnection('ready');
        } else if (error instanceof ApiError && error.status === 401) {
          flush(); source.close(); setAuthenticated(false); setLoginError('登录已过期或服务已重启，请重新输入访问口令。');
          setCredentials(emptyCredentials); keysRef.current = emptyCredentials;
        }
      }).finally(() => { checkingStatus = false; });
    };
    return () => { disposed = true; source.close(); flush(); };
    // Events advance the replay cursor without replacing an open connection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.id, active?.server_id, active?.branch_id, authenticated, streamEpoch, update]);

  useEffect(() => { if (running) { const timer = setInterval(() => setClock(Date.now()), 1000); return () => clearInterval(timer); } }, [running]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      if (follow.current && viewport.current) {
        viewport.current.scrollTop = viewport.current.scrollHeight;
        lastScrollTop.current = viewport.current.scrollTop;
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [active?.events, activeId]);
  useEffect(() => {
    if (input.current) { input.current.style.height = 'auto'; input.current.style.height = `${Math.min(input.current.scrollHeight, 180)}px`; }
  }, [draft]);

  async function login(event: React.FormEvent) {
    event.preventDefault(); setChecking(true); setLoginError('');
    try { await api('/auth/login', 'POST', { password }); setPassword(''); await connect(); }
    catch (error) { setLoginError(errorMessage(error)); }
    finally { setChecking(false); }
  }
  async function send(prompt = draft, query?: string, retry = false) {
    let chat = chatsRef.current.find(item => item.id === activeId);
    if (!catalog || chat && isRunning(chat) || submitting) return;
    let body = retry && chat ? pending.current.get(chat.id) : undefined;
    if (!body && !prompt.trim()) return;
    if (!body && !available?.available) {
      setNotice(available ? availabilityHint(available, preferences.language) : '不可用'); setShowSettings(true); return;
    }
    if (!body && !configured.has('api_key')) {
      setNotice('请先在设置中输入模型 API Key。'); setShowSettings(true); return;
    }
    if (!chat) {
      chat = { id: crypto.randomUUID(), title: t('新会话'), created: new Date().toISOString(), agent: selectedAgent,
        settings: settingsFor(selectedAgent, preferences, catalog), events: [], readonly: false, branch_id: crypto.randomUUID() };
      commit([...chatsRef.current, chat]); setActiveId(chat.id); setSearch('');
    }
    if (query && !retry) { chat = fork(chat, query); update(chat.id, () => chat!); }
    body ??= { request_id: crypto.randomUUID(), prompt: prompt.trim(), settings: chat.settings, credentials: { ...credentials } };
    pending.current.set(chat.id, body);
    setSubmitting(true); setNotice('');
    try {
      if (!body.server_id) {
        const payload = { agent: chat.agent, events: chat.events, source_session: chat.server_id ?? chat.source_id };
        let session: SessionView;
        try { session = await api<SessionView>('/sessions', 'POST', payload); }
        catch (error) {
          if (!(error instanceof ApiError) || error.status !== 404) throw error;
          session = await api<SessionView>('/sessions', 'POST', { ...payload, source_session: undefined });
        }
        body.server_id = session.id;
        setRemembered(value => ({ ...value, [session.id]: session.configured_credentials }));
        update(chat.id, item => ({ ...item, server_id: session.id, source_id: undefined }));
        if (session.recovery_warning) setNotice('旧记录缺少原生检查点，已恢复可用对话；缺失的工具附件需重新获取。');
      }
      const { server_id: serverId, ...question } = body;
      await api(`/sessions/${serverId}/questions`, 'POST', question);
      pending.current.delete(chat.id); setDraft('');
      setRemembered(value => ({ ...value, [serverId!]: [...new Set([
        ...(value[serverId!] ?? []), ...Object.keys(body.credentials).filter(key => body.credentials[key].trim()),
      ])] }));
      update(chat.id, item => ({ ...item, live: true }));
      follow.current = true; setAtBottom(true); setStreamEpoch(n => n + 1);
    } catch (error) {
      if (error instanceof ApiError && error.status > 0) pending.current.delete(chat.id);
      setNotice(errorMessage(error));
      if (error instanceof ApiError && error.status === 422 && !credentials.api_key) setShowSettings(true);
      setStreamEpoch(n => n + 1);
    } finally { setSubmitting(false); }
  }
  async function stop() {
    if (!active?.server_id) return;
    try { await api(`/sessions/${active.server_id}/stop`, 'POST'); setStreamEpoch(n => n + 1); }
    catch (error) { setNotice(errorMessage(error)); }
  }
  function changeAgent(agent: Agent) {
    if (running || !catalog) return;
    const target = catalog.agents.find(item => item.id === agent);
    if (!target || !validation.agent(agent).available) return;
    const next = { ...preferences, agent };
    changePreferences(next);
    if (active) update(active.id, chat => ({ ...chat, agent, settings: settingsFor(agent, next, catalog) }));
  }
  async function renameChat(event: React.FormEvent) {
    event.preventDefault(); if (!rename || !renameText.trim()) return;
    const title = publicData(renameText.trim(), Object.values(credentials));
    try {
      update(rename.id, chat => ({ ...chat, title, renamed: true })); setRename(null);
    } catch (error) { setNotice(errorMessage(error)); }
  }
  async function removeChat() {
    if (!remove) return;
    try {
      if (remove.server_id ?? remove.source_id) {
        try { await api(`/sessions/${remove.server_id ?? remove.source_id}`, 'DELETE'); }
        catch (error) { if (!(error instanceof ApiError) || error.status !== 404) throw error; }
      }
      setRemoving(items => [...items, remove.id]); setRemove(null);
      await new Promise(resolve => setTimeout(resolve, matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 220));
      await deleteChat(remove.id);
      setRemoving(items => items.filter(id => id !== remove.id));
      const next = chatsRef.current.filter(chat => chat.id !== remove.id);
      commit(next, false); pending.current.delete(remove.id);
      if (activeId === remove.id) { setActiveId(next.at(-1)?.id ?? ''); setDraft(''); }
      setRemove(null);
      if (!next.length) newChat();
    } catch (error) { setNotice(errorMessage(error)); }
  }
  async function importFile(file?: File) {
    if (!file) return;
    try {
      if (file.size > 32 * 1024 * 1024) throw new Error('导入文件不能超过 32 MB');
      const imported = importHistory(JSON.parse(await file.text()));
      commit([...chatsRef.current, ...imported]);
      if (imported.length) setActiveId(imported[0].id);
      setNotice(preferences.language === 'zh' ? `已导入 ${imported.length} 个会话。` : `Imported ${imported.length} conversations.`);
    } catch { setNotice('导入失败：请选择本应用导出的历史 JSON。'); }
    finally { if (fileInput.current) fileInput.current.value = ''; }
  }
  async function logout() {
    try {
      await api('/auth/logout', 'POST'); setAuthenticated(false); setPassword('');
      setCredentials(emptyCredentials); keysRef.current = emptyCredentials;
      pending.current.clear(); setRemembered({});
      commit(chatsRef.current.map(chat => ({ ...chat, live: false, server_id: undefined, source_id: undefined })));
    } catch (error) { setNotice(errorMessage(error)); }
  }

  function changePreferences(next: Preferences, keys = keysRef.current) {
    setCredentials(keys); keysRef.current = keys;
    if (next === preferences || !catalog) return;
    const clean = publicData(next);
    setPreferences(clean);
    void saveSettings(clean).catch(() => setNotice('设置保存失败，请检查浏览器存储。'));
    const configsChanged = ['connection', 'model', 'agents', 'tools'].some(key => JSON.stringify(clean[key as keyof Preferences]) !== JSON.stringify(preferences[key as keyof Preferences]));
    if (configsChanged) commit(chatsRef.current.map(chat => ({ ...chat, settings: settingsFor(chat.agent, clean, catalog) })));
  }
  function changeModel(value: Partial<ModelSettings>) {
    changePreferences({ ...preferences, model: { ...preferences.model, ...value } });
  }

  if (!authenticated) return <LanguageContext.Provider value={preferences.language}><main className="login-page"><div className="login-card">
    <div className="login-brand"><Mark /><span>{t('循迹')}</span></div><div className="eyebrow">AGENT CHAT</div>
    <h1>{t('从问题出发，')}<br /><span>{t('循证而答。')}</span></h1>
    <p className="login-description">{t('你的私人研究空间。连接代码、社区与网络，')}<br />{t('让每一个回答有迹可循。')}</p>
    <form onSubmit={login}><label>{t('访问口令')}<PasswordInput aria-label={t('访问口令')} autoComplete="current-password" required
      value={password} onChange={e => setPassword(e.target.value)} placeholder={t('输入私人访问口令')} /></label>
      <button className="primary" disabled={checking} type="submit">{checking ? <><LoaderCircle size={17} className="spin" />{t('正在连接服务')}</> : <>{t('进入工作空间')}<ArrowUp size={17} /></>}</button>
    </form>{loginError && <div className="login-error" role="alert"><CircleAlert size={16} />{t(loginError)}</div>}
  </div><span className="login-corner">{t('循迹 / TRACE THE EVIDENCE')}</span></main></LanguageContext.Provider>;

  const modelOptions = [...new Set([composerSettings?.model ?? '', ...discovery.models])].filter(Boolean).map(value => ({ value, label: value }));
  const effort = composerSettings?.thinking ? composerSettings.reasoning_effort : 'off';
  const efforts = [{ value: 'off', label: t('关闭思考') }, ...['low', 'high', 'max'].map(value => ({ value, label: value[0].toUpperCase() + value.slice(1) }))];
  if (effort && !efforts.some(item => item.value === effort)) efforts.push({ value: effort, label: effort });
  return <LanguageContext.Provider value={preferences.language}><div className={`app ${sidebar ? '' : 'sidebar-hidden'} ${resizing ? 'resizing' : ''}`} style={{ '--sidebar-width': `${preferences.sidebar_width}px` } as CSSProperties}>
    <button className={`sidebar-scrim ${sidebar ? 'open' : ''}`} aria-label={t('关闭侧栏')} aria-hidden={!sidebar}
      tabIndex={sidebar ? 0 : -1} onClick={() => setSidebar(false)} />
    <aside className={`sidebar ${sidebar ? 'open' : ''}`} inert={!sidebar}>
      <div className="sidebar-brand"><button className="brand-button" onClick={() => newChat()}><Mark small /><strong>{t('循迹')}</strong><span>Agent Chat</span></button>
        <button className="icon-button" aria-label={t('折叠侧栏')} onClick={() => setSidebar(false)}><PanelLeftClose size={19} /></button></div>
      <button className="new-chat" onClick={() => newChat()}><MessageSquarePlus size={19} />{t('新建会话')}<span>＋</span></button>
      <div className="history-search"><Search size={16} /><input ref={searchInput} aria-label={t('搜索历史')} placeholder={t('搜索历史')} value={search} onChange={e => setSearch(e.target.value)} />
        {search && <button className="icon-button" aria-label={t('清空搜索')} onClick={() => setSearch('')}><X size={14} /></button>}</div>
      <div className="sidebar-label"><History size={13} /><span>{t('会话记录')}</span><span>{chats.length}</span></div>
      <nav className="chat-list" aria-label={t('会话列表')}>{visibleChats.map(chat => <div key={chat.id} className={`chat-row ${chat.id === activeId ? 'active' : ''} ${removing.includes(chat.id) ? 'removing' : ''}`}>
        <button className="chat-open" onClick={() => { setActiveId(chat.id); setDraft(''); setNotice(''); follow.current = true; setAtBottom(true); if (window.innerWidth <= 760) setSidebar(false); }}>
          <span>{chat.title}</span><small>{chat.agent}{isRunning(chat) ? ` · ${t('处理中')}` : ''}</small></button>
        <div className="chat-row-actions"><button className="icon-button" aria-label={`${t('重命名')} ${chat.title}`} onClick={() => { setRename(chat); setRenameText(chat.title); }}><Pencil size={13} /></button>
          <button className="icon-button" aria-label={`${t('删除')} ${chat.title}`} onClick={() => setRemove(chat)}><Trash2 size={13} /></button></div>
      </div>)}{!visibleChats.length && <p className="empty-search">{t('没有匹配的会话')}</p>}</nav>
      <div className="sidebar-bottom"><div className="history-actions"><button onClick={() => fileInput.current?.click()}><Upload size={15} />{t('导入')}</button>
        <button onClick={() => download('chat-history.json', exportHistory(chats))}><Download size={15} />{t('导出历史')}</button></div>
        <input ref={fileInput} type="file" accept="application/json,.json" hidden aria-label={t('导入历史文件')} onChange={e => void importFile(e.target.files?.[0])} />
        <button className="sidebar-setting" aria-label={t('打开设置')} onClick={() => setShowSettings(true)}><Settings2 size={18} />{t('设置')}</button>
        <div className="private-profile"><div className="avatar"><Mark small /></div><div><strong>{t('私人工作空间')}</strong><small>{t('历史保存在此浏览器')}</small></div>
          <button className="icon-button" aria-label={t('退出登录')} onClick={() => void logout()}><LogOut size={17} /></button></div>
      </div>
      <div className="sidebar-resizer" role="separator" aria-label={t('调整侧栏宽度')} aria-orientation="vertical" tabIndex={0}
        aria-valuemin={220} aria-valuemax={480} aria-valuenow={preferences.sidebar_width}
        onKeyDown={e => { if (['ArrowLeft', 'ArrowRight'].includes(e.key)) {
          e.preventDefault(); changePreferences({ ...preferences, sidebar_width: Math.max(220, Math.min(480, preferences.sidebar_width + (e.key === 'ArrowRight' ? 10 : -10))) });
        } }} onPointerDown={e => { e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId); setResizing(true); }}
        onPointerMove={e => { if (e.currentTarget.hasPointerCapture(e.pointerId)) changePreferences({ ...preferences, sidebar_width: Math.max(220, Math.min(480, e.clientX)) }); }}
        onPointerUp={e => { e.currentTarget.releasePointerCapture(e.pointerId); setResizing(false); }} onLostPointerCapture={() => setResizing(false)} />
    </aside>
    <main className="main-panel"><header className="main-header"><div className="header-left">
      {!sidebar && <button className="icon-button" aria-label={t('打开侧栏')} onClick={() => setSidebar(true)}><Menu size={21} /></button>}
      <span className="header-name">{capability?.name ?? active?.agent ?? '循迹'}<ChevronDown size={14} /></span>
      <span className="header-divider">/</span><span className="header-title">{active?.title ?? '循迹'}</span></div>
      <div className="header-right"><span className="status-pill"><i />{t('私人会话')}</span>
        {Boolean(active?.events.length) && <button className="icon-button" aria-label={t('导出事件')} title={t('导出事件')}
          onClick={() => download(`events_${active!.id}.json`, exportEvents(active!))}><Download size={18} /></button>}
        <button className="icon-button theme-toggle" aria-label={t(theme === 'dark' ? '切换为浅色主题' : '切换为深色主题')}
          onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button></div>
    </header>
    <div className="conversation-scroll" ref={viewport}
      onWheel={e => { if (e.deltaY < 0) pauseFollow(); }}
      onTouchStart={e => { touchY.current = e.touches[0]?.clientY ?? null; }}
      onTouchMove={e => {
        const y = e.touches[0]?.clientY;
        if (y !== undefined && touchY.current !== null && y > touchY.current) pauseFollow();
        touchY.current = y ?? null;
      }}
      onKeyDown={e => { if (['ArrowUp', 'PageUp', 'Home'].includes(e.key)) pauseFollow(); }}
      onScroll={() => {
      const element = viewport.current!;
      const bottom = element.scrollHeight - element.scrollTop - element.clientHeight < 90;
      if (element.scrollTop < lastScrollTop.current - 1 && !bottom) follow.current = false;
      else if (element.scrollTop > lastScrollTop.current + 1 && bottom) follow.current = true;
      lastScrollTop.current = element.scrollTop;
      setAtBottom(bottom);
    }}><div className="conversation">
      {!turns.length ? <div className="welcome"><div className="welcome-mark"><Mark /></div><div className="eyebrow">A LITTLE CURIOSITY GOES A LONG WAY</div>
        <h1>{t('今天，想探究什么？')}</h1><p>{t('从一个问题开始，沿着证据找到答案。')}</p>
      </div> : turns.map((turn, index) => {
        const variants = versions(active!, index);
        const selected = variants.findIndex(branch => branch.events.some(event => event.type === 'query/start' && event.query_id === turn.id));
        return <TurnView key={turn.id} turn={turn} interrupted={!running} clock={clock} busy={running || submitting}
          onEdit={text => void send(text, turn.id)} onRegenerate={() => void send(turn.prompt, turn.id)}
          version={{ index: selected, count: variants.length }} onVersion={position => {
            update(active!.id, chat => selectBranch(chat, variants[position])); setDraft('');
          }} />;
      })}
    </div></div>
    <div className="composer-region">
      {!atBottom && <button className="jump-bottom" aria-label={t('回到底部')} onClick={() => { follow.current = true; setAtBottom(true); viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: 'auto' }); }}><ArrowDown size={18} /></button>}
      {connection === 'reconnecting' && <div className="connection-notice" role="status"><LoaderCircle className="spin" size={14} />{t('连接中断，正在重连；查询会在服务端继续运行。')}</div>}
      {notice && <div className="notice" role="status"><CircleAlert size={15} /><span>{t(notice)}</span><button className="icon-button" aria-label={t('关闭提示')} onClick={() => setNotice('')}><X size={15} /></button></div>}
      {active && pending.current.has(active.id) && !submitting && <div className="notice"><span>{t('提交状态未确认，可安全重试同一条问题。')}</span><button className="secondary" onClick={() => void send('', undefined, true)}>{t('重试提交')}</button></div>}
      <form className="composer" onSubmit={e => { e.preventDefault(); void send(); }}>
        <textarea ref={input} aria-label={t('输入问题')} placeholder={t('提出问题，一起循迹…')} rows={2}
          disabled={submitting} maxLength={16000} value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (!running) void send(); }
          }} />
        <div className="composer-tools"><CompactSelect label={t('选择 agent')} disabled={running || submitting}
          value={selectedAgent} onChange={changeAgent}
          options={catalog?.agents.map(item => {
            const status = validation.agent(item.id);
            return { value: item.id, label: item.name, disabled: !status.available, reason: availabilityHint(status, preferences.language) };
          }) ?? []} />
          <div className="composer-right"><CompactSelect className="model-select" label={t('模型名')} value={composerSettings?.model ?? ''}
            options={modelOptions} onChange={model => changeModel({ model })} disabled={running || submitting} searchable
            onRefresh={() => void discovery.refresh()} status={discovery.status === 'loading' ? t('检测模型中…') : discovery.status === 'error' ? t(discovery.error) :
              discovery.status === 'idle' && !discovery.models.length ? t('请配置模型连接以获取列表') : undefined} />
          <CompactSelect className="effort-select" label={t('思考强度')} value={effort ?? 'high'} options={efforts} disabled={running || submitting}
            onChange={value => changeModel(value === 'off' ? { thinking: false } : { thinking: true, reasoning_effort: value })} />
          {running ? <button type="button" className="send-button stop" aria-label={t('停止生成')} onClick={() => void stop()}><Square size={15} fill="currentColor" /></button> :
            <button className="send-button" aria-label={t('发送问题')} type="submit" disabled={!draft.trim() || submitting || !available?.available}
              title={available && !available.available ? availabilityHint(available, preferences.language) : undefined}>
              {submitting ? <LoaderCircle size={19} className="spin" /> : <ArrowUp size={21} />}</button>}</div></div>
      </form><p className="composer-caption"><span>{t('Enter 发送 · Shift + Enter 换行')}</span><span>{t('以来源为依据，保留自己的判断')}</span></p>
    </div></main>
    {showSettings && catalog && <SettingsPanel preferences={preferences} credentials={credentials} catalog={catalog}
      configured={configured} discovery={discovery} validation={validation}
      onDraft={(key, reason) => setConfigDrafts(value => {
        const next = { ...value }; if (reason) next[key] = reason; else delete next[key]; return next;
      })} onClose={() => { setShowSettings(false); setConfigDrafts({}); }} onChange={changePreferences} />}
    {rename && <Modal title={t('重命名会话')} onClose={() => setRename(null)}><form onSubmit={renameChat}>
      <label>{t('会话标题')}<input autoFocus required maxLength={100} value={renameText} onChange={e => setRenameText(e.target.value)} /></label>
      <footer className="modal-actions"><button type="button" className="secondary" onClick={() => setRename(null)}>{t('取消')}</button><button className="primary" type="submit"><Check size={16} />{t('保存标题')}</button></footer>
    </form></Modal>}
    {remove && <Modal title={t('删除会话')} onClose={() => setRemove(null)}><p>{t('删除')} “{remove.title}”。{t('正在执行的任务会停止。')}</p>
      <footer className="modal-actions"><button className="secondary" onClick={() => setRemove(null)}>{t('取消')}</button><button className="danger" onClick={() => void removeChat()}><Trash2 size={16} />{t('确认删除')}</button></footer>
    </Modal>}
  </div></LanguageContext.Provider>;
}
