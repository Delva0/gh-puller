import { Children, isValidElement, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Check, ChevronDown, Copy, LoaderCircle, X, CircleAlert, Terminal, Brain, Square } from 'lucide-react';
import type { Capability, ChatEvent, Credentials, Settings } from './types';

export function Mark({ small = false }: { small?: boolean }) {
  return <svg className={small ? 'brand-mark small' : 'brand-mark'} viewBox="0 0 40 40" fill="none" aria-hidden="true">
    <path d="M8 29 19 7l13 10-11 16L8 29Z" stroke="currentColor" strokeWidth="1.6" />
    <path d="m8 29 14-12 10 0M19 7l3 10-1 16" stroke="currentColor" strokeWidth="1.6" />
    <circle cx="22" cy="17" r="3" fill="currentColor" />
  </svg>;
}

export function Modal({ title, children, onClose, wide = false }: {
  title: string; children: ReactNode; onClose: () => void; wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} className={`modal ${wide ? 'wide' : ''}`} aria-labelledby={id}
    onCancel={e => { e.preventDefault(); onClose(); }} onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="modal-inner"><header><h2 id={id}>{title}</h2>
      <button className="icon-button" onClick={onClose} aria-label="关闭"><X size={20} /></button></header>
      {children}
    </div>
  </dialog>;
}

function nodeText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return Children.toArray(node).map(child => isValidElement(child) ? nodeText(child) : String(child)).join('');
}
export function CopyButton({ text, label = '复制' }: { text: string; label?: string }) {
  const [state, setState] = useState('');
  useEffect(() => { if (state) { const timer = setTimeout(() => setState(''), 2000); return () => clearTimeout(timer); } }, [state]);
  return <button className="copy-button" aria-label={label} title={label} onClick={async () => {
    try { await navigator.clipboard.writeText(text); setState('已复制'); } catch { setState('复制失败'); }
  }}>{state === '已复制' ? <Check size={14} /> : <Copy size={14} />}<span>{state || label}</span></button>;
}
export function MarkdownBody({ text }: { text: string }) {
  return <div className="markdown"><Markdown skipHtml remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}
    components={{
      a: ({ children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>,
      img: ({ src, alt }) => <a href={src} target="_blank" rel="noopener noreferrer">{alt || '查看图片'}</a>,
      pre: ({ children }) => <div className="code-block"><div className="code-toolbar"><span>代码</span>
        <CopyButton text={nodeText(children)} label="复制代码" /></div><pre>{children}</pre></div>,
      table: ({ children }) => <div className="table-scroll"><table>{children}</table></div>,
    }}>{text}</Markdown></div>;
}

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object'
  ? value as Record<string, unknown> : {};
const str = (value: unknown): string => typeof value === 'string' ? value : '';
const pretty = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
function outputText(output: unknown, type: string): string {
  return (Array.isArray(output) ? output : []).filter(item => record(item).type === type).map(item =>
    (Array.isArray(item.content) ? item.content : []).map((part: unknown) => str(record(part).text)).join(''),
  ).join('\n');
}
export interface TraceModel { id: string; model: string; reasoning: string; text: string; error: string }
export interface TraceTool { id: string; name: string; args: unknown; result?: unknown; error?: unknown }
export interface Turn {
  id: string; prompt: string; started: string; models: TraceModel[]; tools: TraceTool[];
  stage?: string;
  end?: { status: string; answer: string; error: string; duration: number };
}
export function turnsFrom(events: ChatEvent[]): Turn[] {
  const turns = new Map<string, Turn>();
  for (const event of events) {
    if (!event.query_id) continue;
    const d = event.data;
    if (event.type === 'query/start') {
      turns.set(event.query_id, { id: event.query_id, prompt: str(d.prompt), started: event.at, models: [], tools: [] });
    }
    const turn = turns.get(event.query_id);
    if (!turn) continue;
    if (event.type === 'query/status') turn.stage = str(d.message);
    if (event.type.startsWith('model/')) {
      const id = str(d.requestId);
      let model = turn.models.find(m => m.id === id);
      if (!model) { model = { id, model: str(d.model), reasoning: '', text: '', error: '' }; turn.models.push(model); }
      if (d.model) model.model = str(d.model);
      if (event.type === 'model/delta/text') model.text += str(d.text);
      if (event.type === 'model/delta/reasoning') model.reasoning += str(d.text);
      if (event.type === 'model/response') {
        model.text = outputText(d.output, 'message');
        model.reasoning = outputText(d.output, 'reasoning');
      }
      if (event.type === 'model/error') model.error = pretty(d.error);
    }
    if (event.type === 'tool/start') turn.tools.push({ id: str(d.callId), name: str(d.name), args: d.arguments });
    if (event.type === 'tool/end') {
      const tool = turn.tools.find(t => t.id === d.callId);
      if (tool) { tool.result = d.result; tool.error = d.error; }
    }
    if (event.type === 'query/end') turn.end = {
      status: str(d.status), answer: str(d.answer), error: str(d.error), duration: Number(d.duration_ms ?? 0),
    };
  }
  return [...turns.values()];
}

export function TurnView({ turn, readonly, clock }: { turn: Turn; readonly: boolean; clock: number }) {
  const running = !turn.end && !readonly;
  const state = turn.end?.status === 'completed' ? '已完成' : turn.end?.status === 'cancelled' ? '已停止' :
    turn.end ? '执行失败' : readonly ? '执行中断' : '处理中';
  const duration = turn.end?.duration ?? (readonly ? 0 : Math.max(0, clock - Date.parse(turn.started)));
  const answer = turn.end?.answer || turn.models.at(-1)?.text || '';
  return <article className="turn" data-testid="turn">
    <div className="user-message"><div>{turn.prompt}</div></div>
    <div className="assistant-message">
      <details className="execution">
        <summary><span className="execution-state">{running ? <LoaderCircle size={15} className="spin" /> :
          turn.end?.status === 'failed' ? <CircleAlert size={15} /> : <Check size={15} />}{state}
          {duration > 0 && <span> · {(duration / 1000).toFixed(1)} 秒</span>}</span><ChevronDown size={15} /></summary>
        <div className="trace">
          <div className="trace-count">{turn.models.length} 次模型请求 · {turn.tools.length} 次工具调用
            {turn.tools.some(t => t.error !== undefined) && ` · ${turn.tools.filter(t => t.error !== undefined).length} 次失败`}</div>
          {turn.models.map((model, index) => <details className="trace-item" key={model.id}>
            <summary><Brain size={15} /><span>思考 {index + 1}</span><small>{model.model}</small><ChevronDown size={14} /></summary>
            <div className="trace-body"><pre>{model.reasoning || '模型未返回 reasoning 内容'}</pre>
              {model.error && <pre className="error-text">{model.error}</pre>}</div>
          </details>)}
          {turn.tools.map(tool => <details className="trace-item" key={tool.id}>
            <summary><Terminal size={15} /><span>{tool.name}</span><small className={tool.error !== undefined ? 'error-text' : ''}>
              {tool.error !== undefined ? '失败' : tool.result !== undefined ? '完成' : running ? '执行中' : '未完成'}</small><ChevronDown size={14} /></summary>
            <div className="trace-body"><h4>参数</h4><pre>{pretty(tool.args)}</pre><h4>结果</h4>
              <pre className={tool.error !== undefined ? 'error-text' : ''}>{pretty(tool.error ?? tool.result) || '尚无结果'}</pre></div>
          </details>)}
          {!turn.models.length && <p className="muted">{running ? turn.stage || '准备查询与模型连接' : '未记录模型请求'}</p>}
        </div>
      </details>
      {answer && <MarkdownBody text={answer} />}
      {turn.end?.error && <div className="turn-notice" role="status">{turn.end.status === 'cancelled' ? <Square size={13} /> : <CircleAlert size={15} />}{turn.end.error}</div>}
      {readonly && !turn.end && <div className="turn-notice">会话已失效，保留最后收到的输出。</div>}
      {turn.end && answer && <div className="answer-actions"><CopyButton text={answer} label="复制回答" /></div>}
    </div>
  </article>;
}

export function SettingsPanel({ settings, credentials, capability, locked, onSave, onClose, remembered }: {
  settings: Settings; credentials: Credentials; capability?: Capability; locked: boolean;
  onSave: (settings: Settings, credentials: Credentials) => void; onClose: () => void; remembered: boolean;
}) {
  const [value, setValue] = useState(settings);
  const [keys, setKeys] = useState(credentials);
  const field = <K extends keyof Settings>(key: K, next: Settings[K]) => setValue(v => ({ ...v, [key]: next }));
  const keyField = (key: keyof Credentials, label: string) => <label>{label}<input type="password" autoComplete="off"
    value={keys[key]} onChange={e => setKeys({ ...keys, [key]: e.target.value })} placeholder={remembered ? '当前服务端会话已保留，留空可继续使用' : '仅当前页面与活跃会话保留'} /></label>;
  return <Modal title="设置" wide onClose={onClose}><form onSubmit={e => { e.preventDefault(); onSave(value, keys); }}>
    <p className="settings-note">密钥仅保留在当前页面和服务端活跃会话内存，刷新页面后不会回填。</p>
    <section className="settings-section"><h3>模型连接</h3>
      <label>模型地址<input type="url" required value={value.base_url} onChange={e => field('base_url', e.target.value)} placeholder="https://api.example.com/v1" /></label>
      <div className="form-grid"><label>模型名<input required value={value.model} onChange={e => field('model', e.target.value)} /></label>
        {keyField('api_key', '模型 API Key')}</div>
      <div className="form-grid three"><label>最大步数<input type="number" min="1" max="128" value={value.max_steps} onChange={e => field('max_steps', +e.target.value)} /></label>
        <label>最大输出 tokens<input type="number" min="256" max="32768" value={value.max_tokens} onChange={e => field('max_tokens', +e.target.value)} /></label>
        <label>思考强度<select value={value.reasoning_effort} onChange={e => field('reasoning_effort', e.target.value as Settings['reasoning_effort'])}>
          <option value="low">Low</option><option value="high">High</option><option value="max">Max</option></select></label></div>
      <label className="check-label"><input type="checkbox" checked={value.thinking} onChange={e => field('thinking', e.target.checked)} />启用模型思考</label>
    </section>
    <section className="settings-section"><h3>Agent 与工具</h3>
      {locked && <p className="settings-note">当前会话的工具配置已固定；新建会话可调整。模型配置和密钥仍可更新。</p>}
      <div className="form-grid three">
        {Boolean(capability?.backends.length) && <label>查询后端<select disabled={locked} value={value.backend} onChange={e => field('backend', e.target.value)}>
          {capability!.backends.map(backend => <option key={backend} value={backend}>{backend.toUpperCase()}</option>)}</select></label>}
        <label>工具并发<input type="number" disabled={locked} min="1" max="16" value={value.concurrency} onChange={e => field('concurrency', +e.target.value)} /></label>
        <label>PTC<select disabled={locked || !capability?.ptc} value={value.ptc} onChange={e => field('ptc', e.target.value as Settings['ptc'])}>
          <option value="off">关闭{!capability?.ptc ? '（不可用）' : ''}</option><option value="A">A</option><option value="B">B</option></select></label>
      </div>
      {(capability?.id === 'github' || capability?.id === 'code') && keyField('github_token', 'GitHub Token')}
      {capability?.id === 'gitcode' && keyField('gitcode_token', 'GitCode Token')}
      {capability?.web && <>
        <div className="form-grid"><label>搜索服务<select disabled={locked} value={value.web_search_backend}
          onChange={e => field('web_search_backend', e.target.value as Settings['web_search_backend'])}>
          <option value="brave">Brave（需要 API Key）</option><option value="auto">Auto</option><option value="duckduckgo">DuckDuckGo</option></select></label>
          {value.web_search_backend !== 'duckduckgo' && keyField('brave_api_key', 'Brave API Key')}</div>
        <div className="form-grid"><label>搜索并发<input type="number" disabled={locked} min="1" max="8" value={value.web_search_concurrency} onChange={e => field('web_search_concurrency', +e.target.value)} /></label>
          <label>搜索间隔（秒）<input type="number" disabled={locked} min="0" max="60" step="0.1" value={value.web_search_interval} onChange={e => field('web_search_interval', +e.target.value)} /></label></div>
        <label className="check-label"><input type="checkbox" disabled={locked} checked={value.multimodal} onChange={e => field('multimodal', e.target.checked)} />允许向模型提供图片</label>
      </>}
    </section>
    <footer className="modal-actions"><button type="button" className="secondary" onClick={onClose}>取消</button><button className="primary" type="submit">保存设置</button></footer>
  </form></Modal>;
}
