import { Children, isValidElement, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Check, ChevronDown, Copy, LoaderCircle, X, CircleAlert, Terminal, Brain, Square } from 'lucide-react';
import { settingsFor, type Catalog, type ChatEvent, type ConfigField, type ConfigValues, type Credentials,
  type Preferences } from './types';

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
  const outsidePress = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [closing, setClosing] = useState(false);
  const id = useId();
  useEffect(() => { ref.current?.showModal(); return () => clearTimeout(timer.current); }, []);
  function close() {
    if (timer.current) return;
    setClosing(true);
    timer.current = setTimeout(onClose, matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 160);
  }
  return <dialog ref={ref} className={`modal ${wide ? 'wide' : ''} ${closing ? 'closing' : ''}`} aria-labelledby={id}
    onCancel={e => { e.preventDefault(); close(); }}
    onPointerDown={e => {
      const box = e.currentTarget.getBoundingClientRect();
      outsidePress.current = e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom;
    }} onClick={e => { if (outsidePress.current && e.target === e.currentTarget) close(); }}>
    <div className="modal-inner"><header><h2 id={id}>{title}</h2>
      <button className="icon-button" onClick={close} aria-label="关闭"><X size={20} /></button></header>
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
type TraceEntry = { kind: 'model'; value: TraceModel } | { kind: 'tool'; value: TraceTool };
export interface Turn {
  id: string; prompt: string; started: string; models: TraceModel[]; tools: TraceTool[];
  trace: TraceEntry[];
  stage?: string;
  end?: { status: string; answer: string; error: string; duration: number };
}
export function turnsFrom(events: ChatEvent[]): Turn[] {
  const turns = new Map<string, Turn>();
  for (const event of events) {
    if (!event.query_id) continue;
    const d = event.data;
    if (event.type === 'query/start') {
      turns.set(event.query_id, { id: event.query_id, prompt: str(d.prompt), started: event.at, models: [], tools: [], trace: [] });
    }
    const turn = turns.get(event.query_id);
    if (!turn) continue;
    if (event.type === 'query/status') turn.stage = str(d.message);
    if (event.type.startsWith('model/')) {
      const id = str(d.requestId);
      let model = turn.models.find(m => m.id === id);
      if (!model) {
        model = { id, model: str(d.model), reasoning: '', text: '', error: '' };
        turn.models.push(model); turn.trace.push({ kind: 'model', value: model });
      }
      if (d.model) model.model = str(d.model);
      if (event.type === 'model/delta/text') model.text += str(d.text);
      if (event.type === 'model/delta/reasoning') model.reasoning += str(d.text);
      if (event.type === 'model/response') {
        model.text = outputText(d.output, 'message');
        model.reasoning = outputText(d.output, 'reasoning');
      }
      if (event.type === 'model/error') model.error = pretty(d.error);
    }
    if (event.type === 'tool/start') {
      const tool = { id: str(d.callId), name: str(d.name), args: d.arguments };
      turn.tools.push(tool); turn.trace.push({ kind: 'tool', value: tool });
    }
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
          {turn.trace.map(entry => entry.kind === 'model' ? <details className="trace-item" data-kind="model" key={`model-${entry.value.id}`}>
            <summary><Brain size={15} /><span className="trace-title">思考</span>
              <span className="trace-description">{entry.value.reasoning.trimStart().split(/\r?\n/, 1)[0]}</span><ChevronDown size={14} /></summary>
            <div className="trace-body"><pre>{entry.value.reasoning || '模型未返回 reasoning 内容'}</pre>
              {entry.value.error && <pre className="error-text">{entry.value.error}</pre>}</div>
          </details> : <details className="trace-item" data-kind="tool" key={`tool-${entry.value.id}`}>
            <summary><Terminal size={15} /><span className="trace-title">{entry.value.name}</span><span className="trace-description" />
              <small className={entry.value.error !== undefined ? 'error-text' : ''}>
                {entry.value.error !== undefined ? '失败' : entry.value.result !== undefined ? '完成' : running ? '执行中' : '未完成'}</small><ChevronDown size={14} /></summary>
            <div className="trace-body"><h4>参数</h4><pre>{pretty(entry.value.args)}</pre><h4>结果</h4>
              <pre className={entry.value.error !== undefined ? 'error-text' : ''}>{pretty(entry.value.error ?? entry.value.result) || '尚无结果'}</pre></div>
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

const fieldName = (key: string) => key.split('_').map(word => word.length <= 3 ? word.toUpperCase() : word[0].toUpperCase() + word.slice(1)).join(' ');
const choiceName = (value: unknown) => value === false ? '关闭' : value === true ? '开启' : value === null ? '默认' : String(value);

function ConfigInput({ field, value, onChange }: {
  field: ConfigField; value: ConfigValues[string]; onChange: (value: ConfigValues[string]) => void;
}) {
  const format = (v: ConfigValues[string]) => v === null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
  const [text, setText] = useState(format(value));
  useEffect(() => setText(format(value)), [value]);
  const label = fieldName(field.key);
  const shared = { name: field.key, 'aria-label': label };
  let control: ReactNode;
  if (field.choices.length) {
    control = <select {...shared} value={JSON.stringify(value)} onChange={e => onChange(JSON.parse(e.target.value))}>
      {field.choices.map(choice => <option key={JSON.stringify(choice.value)} value={JSON.stringify(choice.value)} disabled={Boolean(choice.reason)}>
        {choiceName(choice.value)}{choice.reason ? ` · ${choice.reason}` : ''}</option>)}
    </select>;
  } else if (field.type === 'boolean') {
    control = <input {...shared} type="checkbox" role="switch" checked={Boolean(value)} onChange={e => onChange(e.target.checked)} />;
  } else if (field.type === 'integer' || field.type === 'number') {
    control = <input {...shared} type="number" step={field.type === 'integer' ? 1 : 'any'} value={text} placeholder={field.nullable ? '默认' : ''}
      onChange={e => {
        setText(e.target.value);
        const next = Number(e.target.value);
        if (!e.target.value && field.nullable) onChange(null);
        else if (e.target.value && Number.isFinite(next) && (field.type !== 'integer' || Number.isInteger(next))) onChange(next);
      }} onBlur={() => setText(format(value))} />;
  } else if (field.type === 'string') {
    control = <input {...shared} value={String(value ?? '')} onChange={e => onChange(e.target.value)} />;
  } else {
    control = <textarea {...shared} value={text} placeholder="JSON" onChange={e => {
      setText(e.target.value);
      try { onChange(JSON.parse(e.target.value)); } catch { /* Keep incomplete JSON in the input only. */ }
    }} onBlur={() => setText(format(value))} />;
  }
  return <label className={`config-field ${field.type === 'boolean' && !field.choices.length ? 'switch-field' : ''}`}>
    <span>{label}</span>{control}{field.description && <small>{field.description}</small>}
  </label>;
}

export function SettingsPanel({ preferences, credentials, catalog, agent, locked, onChange, onClose, remembered }: {
  preferences: Preferences; credentials: Credentials; catalog: Catalog; agent?: string; locked: boolean;
  onChange: (preferences: Preferences, credentials: Credentials) => void; onClose: () => void; remembered: boolean;
}) {
  const [tab, setTab] = useState('global');
  const [selected, setSelected] = useState(agent ?? catalog.default_agent);
  const capability = catalog.agents.find(item => item.id === selected) ?? catalog.agents[0];
  const value = settingsFor(capability.id, preferences, catalog);
  const tools = [...new Map(catalog.agents.flatMap(item => item.tools).map(tool => [tool.id, tool])).values()];
  const toolFields = [...new Map(catalog.agents.flatMap(item => item.fields).filter(field => field.tool).map(field => [field.key, field])).values()];
  const changeAgent = (key: string, next: ConfigValues[string]) => onChange({ ...preferences,
    agents: { ...preferences.agents, [selected]: { ...preferences.agents[selected], [key]: next } } }, credentials);
  const changeTool = (key: string, next: ConfigValues[string]) => onChange({ ...preferences,
    tools: { ...preferences.tools, [key]: next } }, credentials);
  const keyField = (key: string, label: string) => <label key={key}>{label}<input type="password" autoComplete="off" name={key}
    value={credentials[key] ?? ''} onChange={e => onChange(preferences, { ...credentials, [key]: e.target.value })}
    placeholder={remembered ? '当前服务端会话已保留，留空可继续使用' : '仅当前页面与活跃会话保留'} /></label>;
  return <Modal title="设置" wide onClose={onClose}>
    <p className="settings-note">输入即保存。密钥仅保留在当前页面和活跃会话内存。</p>
    <div className="settings-layout"><nav className="settings-tabs" role="tablist" aria-label="设置分类">
      {[['global', '全局'], ['agent', 'Agent'], ['tools', '工具']].map(([id, label]) =>
        <button key={id} type="button" role="tab" aria-selected={tab === id} aria-controls={`settings-${id}`}
          id={`settings-tab-${id}`} onClick={() => setTab(id)}>{label}</button>)}
    </nav><div className="settings-page" key={tab} role="tabpanel" id={`settings-${tab}`} aria-labelledby={`settings-tab-${tab}`}>
      {tab === 'global' && <section><h3>模型连接</h3>
        <label>模型地址<input type="url" value={preferences.connection.base_url ?? catalog.defaults.base_url}
          onChange={e => onChange({ ...preferences, connection: { base_url: e.target.value } }, credentials)}
          placeholder="https://api.example.com/v1" /></label>
        {keyField('api_key', '模型 API Key')}
      </section>}
      {tab === 'agent' && <section><h3>Agent 配置</h3>
        <label>配置 agent<select value={capability.id} onChange={e => setSelected(e.target.value)}>
          {catalog.agents.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        <p className="settings-note">每个 agent 分别保存自己的配置。{locked && '当前会话的工具配置已固定，修改将在新会话生效。'}</p>
        {!capability.available && <p className="settings-note">{capability.reason}</p>}
        {capability.fields.filter(field => !field.tool).map(field => <ConfigInput key={`${selected}-${field.key}`}
          field={field} value={value.options[field.key]} onChange={next => changeAgent(field.key, next)} />)}
        <button className="text-button" type="button" onClick={() => {
          const agents = { ...preferences.agents }; delete agents[selected];
          onChange({ ...preferences, agents }, credentials);
        }}>恢复主包默认配置</button>
      </section>}
      {tab === 'tools' && tools.map(tool => <section className="settings-section" key={tool.id}>
        <h3>{catalog.agents.find(item => item.id === tool.id)?.name ?? fieldName(tool.id)}</h3>
        {tool.credentials.map(key => keyField(key, fieldName(key)))}
        {toolFields.filter(field => field.tool === tool.id).map(field => <ConfigInput key={field.key} field={field}
          value={field.key in preferences.tools ? preferences.tools[field.key] : field.default} onChange={next => changeTool(field.key, next)} />)}
        <p className="settings-note">使用此工具的 agent 共用这些配置；已开始的会话保留原配置。</p>
      </section>)}
    </div></div>
  </Modal>;
}
