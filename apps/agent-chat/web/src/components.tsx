import { Children, isValidElement, useContext, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Check, ChevronDown, ChevronLeft, ChevronRight, Copy, LoaderCircle, X, CircleAlert, Terminal, Brain, Pencil, RotateCcw } from 'lucide-react';
import { type ChatEvent } from './types';
import { LanguageContext, useText } from './i18n';
import { functionDescription, toolLabel } from './ui-model';
import { SourceBadge } from './Sources';
import type { SourceSelection } from './sources';

export function Mark({ small = false, animated = false }: { small?: boolean; animated?: boolean }) {
  const ref = useRef<SVGSVGElement>(null);
  useEffect(() => {
    if (!animated) return;
    const svg = ref.current!;
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, start = 0;
    const draw = (phase: number) => {
      const angle = Math.sin(phase) * .18;
      const points = [[8, 29], [19, 7], [32, 17], [21, 33], [22, 17]].map(([x, y], index) => {
        const offset = index * 1.3;
        const dx = x - 20 + 1.8 * (Math.sin(phase + offset) - Math.sin(offset));
        const dy = y - 20 + 1.5 * (Math.cos(phase + offset) - Math.cos(offset));
        return [20 + dx * Math.cos(angle) - dy * Math.sin(angle), 20 + dx * Math.sin(angle) + dy * Math.cos(angle)];
      });
      svg.children[0].setAttribute('d', `M${points.slice(0, 4).map(point => point.join(' ')).join('L')}Z`);
      svg.children[1].setAttribute('d', `M${points[0]}L${points[4]}L${points[2]}M${points[1]}L${points[4]}L${points[3]}`);
      svg.children[2].setAttribute('cx', String(points[4][0]));
      svg.children[2].setAttribute('cy', String(points[4][1]));
    };
    const tick = (time: number) => {
      start ||= time;
      draw((time - start) % 4000 / 4000 * Math.PI * 2);
      frame = requestAnimationFrame(tick);
    };
    const reset = () => { cancelAnimationFrame(frame); start = 0; draw(0); if (!motion.matches) frame = requestAnimationFrame(tick); };
    reset(); motion.addEventListener('change', reset);
    return () => { cancelAnimationFrame(frame); motion.removeEventListener('change', reset); draw(0); };
  }, [animated]);
  return <svg ref={ref} className={small ? 'brand-mark small' : 'brand-mark'} viewBox="0 0 40 40" fill="none" aria-hidden="true">
    <path d="M8 29 19 7l13 10-11 16L8 29Z" stroke="currentColor" strokeWidth="1.6" />
    <path d="m8 29 14-12 10 0M19 7l3 10-1 16" stroke="currentColor" strokeWidth="1.6" />
    <circle cx="22" cy="17" r="3" fill="currentColor" />
  </svg>;
}

export function Modal({ title, children, onClose, wide = false }: {
  title: string; children: ReactNode; onClose: () => void; wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const t = useText();
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
      <button className="icon-button" onClick={close} aria-label={t('关闭')}><X size={20} /></button></header>
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
  const t = useText();
  const [state, setState] = useState('');
  useEffect(() => { if (state) { const timer = setTimeout(() => setState(''), 2000); return () => clearTimeout(timer); } }, [state]);
  return <button type="button" className="copy-button icon-button" aria-label={t(label)} title={t(state || label)} onClick={async () => {
    try { await navigator.clipboard.writeText(text); setState('已复制'); } catch { setState('复制失败'); }
  }}>{state === '已复制' ? <Check size={15} /> : <Copy size={15} />}<span className="sr-only" role="status">{t(state)}</span></button>;
}
export function MarkdownBody({ text }: { text: string }) {
  const t = useText();
  return <div className="markdown"><Markdown skipHtml remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}
    components={{
      a: ({ children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a>,
      img: ({ src, alt }) => <a href={src} target="_blank" rel="noopener noreferrer">{alt || t('查看图片')}</a>,
      pre: ({ children }) => <div className="code-block"><div className="code-toolbar"><span>{t('代码')}</span>
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

export function TurnView({ turn, interrupted, clock, busy, onEdit, onRegenerate, version, onVersion, sources, onSources }: {
  turn: Turn; interrupted: boolean; clock: number; busy: boolean;
  onEdit: (text: string) => void; onRegenerate: () => void;
  version: { index: number; count: number }; onVersion: (index: number) => void;
  sources: SourceSelection | null; onSources: (selection: SourceSelection | null) => void;
}) {
  const t = useText();
  const language = useContext(LanguageContext);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(turn.prompt);
  const running = !turn.end && !interrupted;
  const state = turn.end?.status === 'completed' ? '已完成' : turn.end?.status === 'cancelled' ? '已停止' :
    turn.end ? '执行失败' : interrupted ? '执行中断' : '处理中';
  const duration = turn.end?.duration ?? (interrupted ? 0 : Math.max(0, clock - Date.parse(turn.started)));
  const answer = turn.end?.answer || [...turn.models].reverse().find(model => model.text.trim())?.text || '';
  return <article className="turn" data-testid="turn">
    {editing ? <form className="message-editor" onSubmit={event => {
      event.preventDefault(); if (draft.trim()) { onEdit(draft.trim()); setEditing(false); }
    }}><textarea aria-label={t('编辑消息')} value={draft} onChange={event => setDraft(event.target.value)} autoFocus rows={4} maxLength={16000} />
      <div><button className="secondary" type="button" onClick={() => setEditing(false)}>{t('取消')}</button>
        <button className="primary" type="submit" disabled={!draft.trim() || busy}>{t('发送')}</button></div></form> :
      <div className="user-message"><div>{turn.prompt}</div></div>}
    <div className="user-actions">
      <CopyButton text={turn.prompt} label="复制问题" />
      <button className="icon-button" aria-label={t('编辑问题')} title={t('编辑问题')} disabled={busy} onClick={() => { setDraft(turn.prompt); setEditing(true); }}><Pencil size={15} /></button>
      {version.count > 1 && <div className="version-control">
        <button className="icon-button" aria-label={t('上一版本')} disabled={busy || version.index <= 0} onClick={() => onVersion(version.index - 1)}><ChevronLeft size={16} /></button>
        <span>{version.index + 1} / {version.count}</span>
        <button className="icon-button" aria-label={t('下一版本')} disabled={busy || version.index >= version.count - 1} onClick={() => onVersion(version.index + 1)}><ChevronRight size={16} /></button>
      </div>}
    </div>
    <div className="assistant-message">
      <details className="execution">
        <summary><span className="execution-state">{running ? <LoaderCircle size={15} className="spin" /> :
          turn.end?.status === 'failed' ? <CircleAlert size={15} /> : <Check size={15} />}{t(state)}
          {duration > 0 && <span> · {(duration / 1000).toFixed(1)} {t('秒')}</span>}</span><ChevronDown size={15} /></summary>
        <div className="trace">
          <div className="trace-count">{turn.models.length} {t('次模型请求')} · {turn.tools.length} {t('次工具调用')}
            {turn.tools.some(tool => tool.error !== undefined) && ` · ${turn.tools.filter(tool => tool.error !== undefined).length} ${t('次失败')}`}</div>
          {turn.trace.filter(entry => entry.kind !== 'model' || entry.value.reasoning.trim()).map(entry => entry.kind === 'model' ? <details className="trace-item" data-kind="model" key={`model-${entry.value.id}`}>
            <summary><Brain size={15} /><span className="trace-title">{t('思考')}</span>
              <span className="trace-description">{entry.value.reasoning.trimStart().split(/\r?\n/, 1)[0]}</span><ChevronDown size={14} /></summary>
            <div className="trace-body"><pre>{entry.value.reasoning}</pre>
              {entry.value.error && <pre className="error-text">{entry.value.error}</pre>}</div>
          </details> : <details className="trace-item" data-kind="tool" key={`tool-${entry.value.id}`}>
            <summary><Terminal size={15} /><span className="trace-title">{toolLabel(entry.value.name, language)}</span>
              <span className="trace-description">{functionDescription(entry.value.name, entry.value.args)}</span>
              <SourceBadge tool={entry.value} selected={sources?.turnId === turn.id && sources.callId === entry.value.id}
                onClick={() => onSources(sources?.turnId === turn.id && sources.callId === entry.value.id
                  ? null : { turnId: turn.id, callId: entry.value.id })} />
              <small className={entry.value.error !== undefined ? 'error-text' : ''}>
                {t(entry.value.error !== undefined ? '失败' : entry.value.result !== undefined ? '完成' : running ? '执行中' : '未完成')}</small><ChevronDown size={14} /></summary>
            <div className="trace-body"><h4>{t('参数')}</h4><pre>{pretty(entry.value.args)}</pre><h4>{t('结果')}</h4>
              <pre className={entry.value.error !== undefined ? 'error-text' : ''}>{pretty(entry.value.error ?? entry.value.result) || t('尚无结果')}</pre></div>
          </details>)}
          {!turn.models.length && <p className="muted">{running ? turn.stage || t('准备查询与模型连接') : t('未记录模型请求')}</p>}
        </div>
      </details>
      {answer && <MarkdownBody text={answer} />}
      {turn.end?.error && turn.end.status !== 'cancelled' && <div className="turn-notice" role="status"><CircleAlert size={15} />{t(turn.end.error)}</div>}
      {interrupted && !turn.end && <div className="turn-notice">{t('保留最后收到的输出，可继续提问。')}</div>}
      {!running && <div className="answer-actions">{answer && <CopyButton text={answer} label="复制回答" />}
        <button className="icon-button" aria-label={t('重新生成')} title={t('重新生成')} disabled={busy} onClick={onRegenerate}><RotateCcw size={16} /></button>
      </div>}
    </div>
  </article>;
}
