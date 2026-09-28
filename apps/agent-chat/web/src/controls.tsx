import { useEffect, useId, useRef, useState, type CSSProperties, type InputHTMLAttributes, type ReactNode } from 'react';
import { Check, ChevronDown, Eye, EyeOff, RefreshCw } from 'lucide-react';
import { useText } from './i18n';

export function PasswordInput({ actions, ...props }: InputHTMLAttributes<HTMLInputElement> & { actions?: ReactNode }) {
  const t = useText();
  const [visible, setVisible] = useState(false);
  return <span className="password-control"><input {...props} type={visible ? 'text' : 'password'} />
    <span className="password-actions">{actions}<button type="button" className="icon-button"
      aria-label={t(visible ? '隐藏密码' : '显示密码')} title={t(visible ? '隐藏密码' : '显示密码')}
      onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={16} /> : <Eye size={16} />}</button></span></span>;
}
export interface SelectOption { value: string; label: string; disabled?: boolean; reason?: string }
export function CompactSelect({ label, value, options, onChange, disabled, className = '', searchable, status, onRefresh }: {
  label: string; value: string; options: SelectOption[]; onChange: (value: string) => void;
  disabled?: boolean; className?: string; searchable?: boolean; status?: string; onRefresh?: () => void;
}) {
  const t = useText();
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const [bottom, setBottom] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    setFilter('');
    const position = () => setBottom(window.innerHeight - (root.current?.getBoundingClientRect().top ?? 0) + 12);
    position(); window.addEventListener('resize', position);
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', close);
    requestAnimationFrame(() => searchable ? search.current?.focus() : root.current?.querySelector<HTMLButtonElement>('[aria-selected=true]')?.focus());
    return () => { document.removeEventListener('pointerdown', close); window.removeEventListener('resize', position); };
  }, [open, searchable]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  const filtered = options.filter(option => option.label.toLowerCase().includes(filter.toLowerCase()));
  return <div className={`compact-select ${className}`} ref={root} onKeyDown={event => {
    if (event.key === 'Escape') { event.preventDefault(); setOpen(false); trigger.current?.focus(); }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      if (!open && !disabled) { event.preventDefault(); setOpen(true); return; }
      if (!open) return;
      event.preventDefault();
      const buttons = [...root.current!.querySelectorAll<HTMLButtonElement>('[role=option]:not(:disabled)')];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 :
        event.key === 'ArrowDown' ? (index + 1) % buttons.length : (index - 1 + buttons.length) % buttons.length;
      buttons[next]?.focus();
    }
  }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false); }}>
    <button ref={trigger} type="button" className="select-trigger" role="combobox" aria-label={label} aria-expanded={open}
      aria-controls={id} aria-haspopup="listbox" disabled={disabled} onClick={() => setOpen(!open)}>
      <span>{options.find(option => option.value === value)?.label ?? value}</span><ChevronDown size={13} /></button>
    {open && <div className="select-popup" style={{ '--picker-bottom': `${bottom}px` } as CSSProperties}>
      {searchable && <div className="model-search"><input ref={search} aria-label={t('搜索模型')} placeholder={t('搜索模型')} value={filter} onChange={e => setFilter(e.target.value)} />
        {onRefresh && <button className="icon-button" type="button" aria-label={t('刷新模型列表')} onClick={onRefresh}><RefreshCw size={14} /></button>}</div>}
      <div id={id} role="listbox" aria-label={label}>{filtered.map(option => <button key={option.value} type="button" role="option"
        aria-selected={option.value === value} disabled={option.disabled} title={option.reason} onClick={() => {
          onChange(option.value); setOpen(false); trigger.current?.focus();
        }}><span>{option.label}{option.disabled && <small>{option.reason || t('不可用')}</small>}</span>{option.value === value && <Check size={15} />}</button>)}</div>
      {!filtered.length && <p className="select-status">{t('没有匹配的模型')}</p>}
      {status && <p className="select-status" role="status">{status}</p>}
    </div>}
  </div>;
}
