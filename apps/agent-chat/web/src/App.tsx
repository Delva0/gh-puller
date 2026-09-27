import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, Check, ChevronDown, CircleAlert, Download, Globe, History, LoaderCircle,
  LogOut, Menu, MessageSquarePlus, PanelLeftClose, Search, Settings2, Square, Trash2, Upload, X, Pencil } from 'lucide-react';
import { api, ApiError } from './api';
import { Mark, Modal, SettingsPanel, TurnView, turnsFrom } from './components';
import { deleteChat, download, exportHistory, importHistory, loadHistory, loadSettings, saveChat, saveSettings } from './db';
import { emptyCredentials, eventSchema, fallbackSettings, isRunning, publicData, settingsFor,
  type Agent, type Catalog, type ChatEvent, type Conversation, type Credentials, type SessionView, type Settings } from './types';

type Pending = { request_id: string; prompt: string; settings: Settings; credentials: Credentials };
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
  const [preferences, setPreferences] = useState(fallbackSettings);
  const [credentials, setCredentials] = useState<Credentials>(emptyCredentials);
  const keysRef = useRef(credentials);
  const [remembered, setRemembered] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState('');
  const [sidebar, setSidebar] = useState(() => window.innerWidth > 760);
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
  const capability = catalog?.agents.find(item => item.id === active?.agent);
  const turns = useMemo(() => turnsFrom(active?.events ?? []), [active?.events]);
  const running = active ? isRunning(active) : false;
  const locked = Boolean(active?.events.length);
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
    commit(chatsRef.current.map(chat => chat.id === id ? publicData(change(chat), Object.values(keysRef.current)) : chat));
  }, [commit]);

  function pauseFollow() {
    if (viewport.current && viewport.current.scrollHeight > viewport.current.clientHeight) {
      lastScrollTop.current = viewport.current.scrollTop;
      follow.current = false; setAtBottom(false);
    }
  }

  function newChat(settings = preferences) {
    const chat: Conversation = { id: crypto.randomUUID(), title: '新会话', created: new Date().toISOString(),
      agent: 'github', settings: settingsFor('github', settings), events: [], readonly: false };
    commit([...chatsRef.current, chat]); setActiveId(chat.id); setDraft(''); setNotice('');
    follow.current = true; setAtBottom(true);
    if (window.innerWidth <= 760) setSidebar(false);
    setTimeout(() => input.current?.focus(), 0);
  }

  async function connect() {
    const [info, sessions, saved] = await Promise.all([
      api<Catalog>('/catalog'), api<SessionView[]>('/sessions'), loadSettings().catch(() => undefined),
    ]);
    setCatalog(info);
    const config = saved ?? info.defaults;
    setPreferences(config);
    setRemembered(Object.fromEntries(sessions.map(session => [session.id, session.has_credentials])));
    commit(chatsRef.current.map(chat => {
      if (!chat.server_id) return chat;
      const session = sessions.find(item => item.id === chat.server_id);
      return { ...chat, readonly: !session || session.readonly };
    }));
    setAuthenticated(true); setLoginError(''); setConnection('ready');
    if (!chatsRef.current.length) newChat(config);
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
    if (!authenticated || !active?.server_id || active.readonly) return;
    const id = active.id;
    const serverId = active.server_id;
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
        const after = chat.events.at(-1)?.seq ?? 0;
        const seen = new Set<number>();
        const entries = batch.filter(e => e.seq > after && !seen.has(e.seq) && seen.add(e.seq)).sort((a, b) => a.seq - b.seq);
        const first = entries.find(e => e.type === 'query/start');
        return { ...chat, events: [...chat.events, ...entries],
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
          setRemembered(value => ({ ...value, [serverId]: session.has_credentials }));
          if (session.readonly) update(id, chat => ({ ...chat, readonly: true }));
        }
      }).catch(() => {});
    });
    source.addEventListener('expired', () => { flush(); source.close(); update(id, chat => ({ ...chat, readonly: true })); });
    source.onerror = () => {
      if (disposed) return;
      setConnection('reconnecting');
      if (checkingStatus) return;
      checkingStatus = true;
      void api<SessionView>(`/sessions/${serverId}`).catch(error => {
        if (disposed) return;
        if (error instanceof ApiError && error.status === 404) {
          flush(); source.close(); update(id, chat => ({ ...chat, readonly: true })); setConnection('ready');
        } else if (error instanceof ApiError && error.status === 401) {
          flush(); source.close(); setAuthenticated(false); setLoginError('登录已过期或服务已重启，请重新输入访问口令。');
          setCredentials(emptyCredentials); keysRef.current = emptyCredentials;
        }
      }).finally(() => { checkingStatus = false; });
    };
    return () => { disposed = true; source.close(); flush(); };
    // Events advance the replay cursor without replacing an open connection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.id, active?.server_id, active?.readonly, authenticated, streamEpoch, update]);

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
  async function send(retry = false) {
    if (!active || active.readonly || running || submitting) return;
    let body = retry ? pending.current.get(active.id) : undefined;
    if (!body && !draft.trim()) return;
    if (!body && !credentials.api_key && !(active.server_id && remembered[active.server_id])) {
      setNotice('请先在设置中输入模型 API Key。'); setShowSettings(true); return;
    }
    body ??= { request_id: crypto.randomUUID(), prompt: draft.trim(), settings: active.settings, credentials: { ...credentials } };
    pending.current.set(active.id, body);
    setSubmitting(true); setNotice('');
    let serverId = active.server_id;
    try {
      if (!serverId) {
        const session = await api<SessionView>('/sessions', 'POST', { agent: active.agent });
        serverId = session.id;
        update(active.id, chat => ({ ...chat, server_id: session.id }));
      }
      await api(`/sessions/${serverId}/questions`, 'POST', body);
      pending.current.delete(active.id); setDraft('');
      setRemembered(value => ({ ...value, [serverId!]: true }));
      follow.current = true; setAtBottom(true); setStreamEpoch(n => n + 1);
    } catch (error) {
      if (error instanceof ApiError && error.status > 0) pending.current.delete(active.id);
      setNotice(errorMessage(error));
      if (error instanceof ApiError && error.status === 404) update(active.id, chat => ({ ...chat, readonly: true }));
      if (serverId) setStreamEpoch(n => n + 1);
    } finally { setSubmitting(false); }
  }
  async function stop() {
    if (!active?.server_id) return;
    try { await api(`/sessions/${active.server_id}/stop`, 'POST'); setStreamEpoch(n => n + 1); }
    catch (error) { setNotice(errorMessage(error)); }
  }
  async function changeAgent(agent: Agent) {
    if (!active || locked) return;
    try {
      if (active.server_id) await api(`/sessions/${active.server_id}`, 'DELETE');
      update(active.id, chat => ({ ...chat, agent, server_id: undefined, settings: settingsFor(agent, chat.settings) }));
    } catch (error) { setNotice(errorMessage(error)); }
  }
  async function renameChat(event: React.FormEvent) {
    event.preventDefault(); if (!rename || !renameText.trim()) return;
    const title = publicData(renameText.trim(), Object.values(credentials));
    try {
      if (rename.server_id && !rename.readonly) await api(`/sessions/${rename.server_id}`, 'PATCH', { title });
      update(rename.id, chat => ({ ...chat, title, renamed: true })); setRename(null);
    } catch (error) { setNotice(errorMessage(error)); }
  }
  async function removeChat() {
    if (!remove) return;
    try {
      if (remove.server_id && !remove.readonly) {
        try { await api(`/sessions/${remove.server_id}`, 'DELETE'); }
        catch (error) { if (!(error instanceof ApiError) || error.status !== 404) throw error; }
      }
      await deleteChat(remove.id);
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
      if (file.size > 20 * 1024 * 1024) throw new Error('导入文件不能超过 20 MB');
      const imported = importHistory(JSON.parse(await file.text()));
      commit([...chatsRef.current, ...imported]);
      if (imported.length) setActiveId(imported[0].id);
      setNotice(`已导入 ${imported.length} 个只读会话。`);
    } catch { setNotice('导入失败：请选择本应用导出的历史 JSON，文件须小于 20 MB。'); }
    finally { if (fileInput.current) fileInput.current.value = ''; }
  }
  async function logout() {
    try {
      await api('/auth/logout', 'POST'); setAuthenticated(false); setPassword('');
      setCredentials(emptyCredentials); keysRef.current = emptyCredentials;
      pending.current.clear(); setRemembered({});
      commit(chatsRef.current.map(chat => ({ ...chat, readonly: chat.events.length > 0 || chat.readonly, server_id: undefined })));
    } catch (error) { setNotice(errorMessage(error)); }
  }

  if (!authenticated) return <main className="login-page"><div className="login-card">
    <div className="login-brand"><Mark /><span>循迹</span></div><div className="eyebrow">AGENT CHAT</div>
    <h1>从问题出发，<br /><span>循证而答。</span></h1>
    <p className="login-description">你的私人研究空间。连接代码、社区与网络，<br />让每一个回答有迹可循。</p>
    <form onSubmit={login}><label>访问口令<input type="password" autoComplete="current-password" required
      value={password} onChange={e => setPassword(e.target.value)} placeholder="输入私人访问口令" /></label>
      <button className="primary" disabled={checking} type="submit">{checking ? <><LoaderCircle size={17} className="spin" />正在连接服务</> : <>进入工作空间<ArrowUp size={17} /></>}</button>
    </form>{loginError && <div className="login-error" role="alert"><CircleAlert size={16} />{loginError}</div>}
    <p className="login-footnote">免费服务唤醒可能需要约一分钟。</p>
  </div><span className="login-corner">循迹 / TRACE THE EVIDENCE</span></main>;

  return <div className={`app ${sidebar ? '' : 'sidebar-hidden'}`}>
    {sidebar && <button className="sidebar-scrim" aria-label="关闭侧栏" onClick={() => setSidebar(false)} />}
    <aside className={`sidebar ${sidebar ? 'open' : ''}`}>
      <div className="sidebar-brand"><button className="brand-button" onClick={() => newChat()}><Mark small /><strong>循迹</strong><span>Agent Chat</span></button>
        <button className="icon-button" aria-label="折叠侧栏" onClick={() => setSidebar(false)}><PanelLeftClose size={19} /></button></div>
      <button className="new-chat" onClick={() => newChat()}><MessageSquarePlus size={19} />新建会话<span>＋</span></button>
      <div className="history-search"><Search size={16} /><input ref={searchInput} aria-label="搜索历史" placeholder="搜索历史" value={search} onChange={e => setSearch(e.target.value)} />
        {search && <button className="icon-button" aria-label="清空搜索" onClick={() => setSearch('')}><X size={14} /></button>}</div>
      <div className="sidebar-label"><History size={13} /><span>会话记录</span><span>{chats.length}</span></div>
      <nav className="chat-list" aria-label="会话列表">{visibleChats.map(chat => <div key={chat.id} className={`chat-row ${chat.id === activeId ? 'active' : ''}`}>
        <button className="chat-open" onClick={() => { setActiveId(chat.id); setDraft(''); setNotice(''); follow.current = true; setAtBottom(true); if (window.innerWidth <= 760) setSidebar(false); }}>
          <span>{chat.title}</span><small>{chat.agent}{chat.readonly ? ' · 只读' : isRunning(chat) ? ' · 处理中' : ''}</small></button>
        <div className="chat-row-actions"><button className="icon-button" aria-label={`重命名 ${chat.title}`} onClick={() => { setRename(chat); setRenameText(chat.title); }}><Pencil size={13} /></button>
          <button className="icon-button" aria-label={`删除 ${chat.title}`} onClick={() => setRemove(chat)}><Trash2 size={13} /></button></div>
      </div>)}{!visibleChats.length && <p className="empty-search">没有匹配的会话</p>}</nav>
      <div className="sidebar-bottom"><div className="history-actions"><button onClick={() => fileInput.current?.click()}><Upload size={15} />导入</button>
        <button onClick={() => download('chat-history.json', exportHistory(chats))}><Download size={15} />导出历史</button></div>
        <input ref={fileInput} type="file" accept="application/json,.json" hidden aria-label="导入历史文件" onChange={e => void importFile(e.target.files?.[0])} />
        <button className="sidebar-setting" onClick={() => setShowSettings(true)}><Settings2 size={18} />设置</button>
        <div className="private-profile"><div className="avatar"><Mark small /></div><div><strong>私人工作空间</strong><small>历史保存在此浏览器</small></div>
          <button className="icon-button" aria-label="退出登录" onClick={() => void logout()}><LogOut size={17} /></button></div>
      </div>
    </aside>
    <main className="main-panel"><header className="main-header"><div className="header-left">
      {!sidebar && <button className="icon-button" aria-label="打开侧栏" onClick={() => setSidebar(true)}><Menu size={21} /></button>}
      <span className="header-name">{active?.agent === 'github' ? 'GitHub' : active?.agent === 'gitcode' ? 'GitCode' : active?.agent === 'code' ? 'Code' : 'Web'}<ChevronDown size={14} /></span>
      <span className="header-divider">/</span><span className="header-title">{active?.title ?? '循迹'}</span></div>
      <div className="header-right">{active?.readonly ? <span className="status-pill">只读历史</span> : <span className="status-pill"><i />私人会话</span>}
        {Boolean(active?.events.length) && <button className="icon-button" aria-label="导出事件" title="导出 events.json" onClick={() => download('events.json', {
          version: 1, session: { title: active!.title, agent: active!.agent, settings: active!.settings }, events: active!.events,
        })}><Download size={18} /></button>}</div>
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
        <h1>今天，想探究什么？</h1><p>从一个问题开始，沿着证据找到答案。</p>
        <div className="agent-availability">{catalog?.agents.map(item => <button key={item.id} disabled={!item.available || locked || submitting}
          className={active?.agent === item.id ? 'chosen' : ''} title={item.reason || item.name} onClick={() => void changeAgent(item.id)}>
          {item.id === 'web' ? <Globe size={15} /> : <span className="agent-dot" />}{item.name}{!item.available && <small>不可用</small>}</button>)}</div>
      </div> : turns.map(turn => <TurnView key={turn.id} turn={turn} readonly={active!.readonly} clock={clock} />)}
      {active?.readonly && <div className="readonly-notice"><History size={17} /><div><strong>此会话为只读历史</strong><p>会话已失效、达到保留上限，或来自导入文件。新建会话后可继续研究。</p></div><button className="secondary" onClick={() => newChat()}>新建会话</button></div>}
    </div></div>
    <div className="composer-region">
      {!atBottom && <button className="jump-bottom" aria-label="回到底部" onClick={() => { follow.current = true; setAtBottom(true); viewport.current?.scrollTo({ top: viewport.current.scrollHeight, behavior: 'auto' }); }}><ArrowDown size={18} /></button>}
      {connection === 'reconnecting' && <div className="connection-notice" role="status"><LoaderCircle className="spin" size={14} />连接中断，正在重连；查询会在服务端继续运行。</div>}
      {notice && <div className="notice" role="status"><CircleAlert size={15} /><span>{notice}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setNotice('')}><X size={15} /></button></div>}
      {active && pending.current.has(active.id) && !submitting && <div className="notice"><span>提交状态未确认，可安全重试同一条问题。</span><button className="secondary" onClick={() => void send(true)}>重试提交</button></div>}
      <form className={`composer ${active?.readonly ? 'disabled' : ''}`} onSubmit={e => { e.preventDefault(); void send(); }}>
        <textarea ref={input} aria-label="输入问题" placeholder={active?.readonly ? '只读历史 · 新建会话开始新的研究' : '提出问题，一起循迹…'} rows={2}
          disabled={active?.readonly || submitting} value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (!running) void send(); }
          }} />
        <div className="composer-tools"><div className="composer-options"><select aria-label="选择 agent" disabled={locked || submitting || active?.readonly}
          value={active?.agent ?? 'github'} onChange={e => void changeAgent(e.target.value as Agent)}>
          {catalog?.agents.map(item => <option key={item.id} value={item.id} disabled={!item.available}>{item.name}{!item.available ? ' · 未配置 Docker' : ''}</option>)}</select>
          <span className="composer-model">{active?.settings.backend ? active.settings.backend.toUpperCase() + ' · ' : ''}{active?.settings.model}</span></div>
          <div className="composer-buttons"><button type="button" className="icon-button" aria-label="打开设置" onClick={() => setShowSettings(true)}><Settings2 size={19} /></button>
            {running ? <button type="button" className="send-button stop" aria-label="停止生成" onClick={() => void stop()}><Square size={15} fill="currentColor" /></button> :
              <button className="send-button" aria-label="发送问题" type="submit" disabled={!draft.trim() || submitting || active?.readonly || !catalog}>
                {submitting ? <LoaderCircle size={19} className="spin" /> : <ArrowUp size={21} />}</button>}</div></div>
      </form><p className="composer-caption"><span>Enter 发送 · Shift + Enter 换行</span><span>以来源为依据，保留自己的判断</span></p>
    </div></main>
    {showSettings && <SettingsPanel settings={active?.settings ?? preferences} credentials={credentials} capability={capability}
      locked={locked && !active?.readonly} remembered={Boolean(active?.server_id && remembered[active.server_id])} onClose={() => setShowSettings(false)}
      onSave={(settings, keys) => {
        setCredentials(keys); keysRef.current = keys;
        const clean = publicData(settings, Object.values(keys));
        setPreferences(clean); void saveSettings(clean).catch(() => setNotice('设置保存失败，请检查浏览器存储。'));
        if (active && !active.readonly) update(active.id, chat => ({ ...chat, settings: clean }));
        setShowSettings(false); setNotice('设置已更新，密钥仅在当前页面与活跃会话中保留。');
      }} />}
    {rename && <Modal title="重命名会话" onClose={() => setRename(null)}><form onSubmit={renameChat}>
      <label>会话标题<input autoFocus required maxLength={100} value={renameText} onChange={e => setRenameText(e.target.value)} /></label>
      <footer className="modal-actions"><button type="button" className="secondary" onClick={() => setRename(null)}>取消</button><button className="primary" type="submit"><Check size={16} />保存标题</button></footer>
    </form></Modal>}
    {remove && <Modal title="删除会话" onClose={() => setRemove(null)}><p>删除“{remove.title}”及此浏览器中的记录。正在执行的任务会停止。</p>
      <footer className="modal-actions"><button className="secondary" onClick={() => setRemove(null)}>取消</button><button className="danger" onClick={() => void removeChat()}><Trash2 size={16} />确认删除</button></footer>
    </Modal>}
  </div>;
}
