import { useEffect, useMemo, useRef, useState } from 'react';
import { ExternalLink, Github, Globe, X } from 'lucide-react';
import type { TraceTool } from './components';
import { useText } from './i18n';
import { toolSources, type Source } from './sources';

function SourceIcon({ source }: { source: Source }) {
  const [failed, setFailed] = useState(false);
  if (source.site === 'GitHub') return <Github size={15} aria-hidden="true" />;
  return !failed ? <img src={new URL('/favicon.ico', source.url).href} alt="" loading="lazy" referrerPolicy="no-referrer"
    onError={() => setFailed(true)} /> : <Globe size={15} aria-hidden="true" />;
}

export function SourceBadge({ tool, selected, onClick }: { tool: TraceTool; selected: boolean; onClick: () => void }) {
  const t = useText();
  const sources = useMemo(() => toolSources(tool), [tool.name, tool.result, tool.error]);
  if (!sources.length) return null;
  return <button type="button" className="source-badge" aria-expanded={selected} aria-controls="source-panel"
    aria-label={`${sources.length} ${t('个来源')}`} onClick={event => { event.preventDefault(); event.stopPropagation(); onClick(); }}>
    <span className="source-icons" aria-hidden="true">{sources.slice(0, 3).map(source => <span key={source.url}><SourceIcon source={source} /></span>)}</span>
    <span>{sources.length} {t('个来源')}</span>
  </button>;
}

export function SourcePanel({ tool, onClose }: { tool?: TraceTool; onClose: () => void }) {
  const t = useText();
  const ref = useRef<HTMLElement>(null);
  const open = Boolean(tool);
  const nextSources = useMemo(() => tool ? toolSources(tool) : null, [tool?.name, tool?.result, tool?.error]);
  // Preserve the last selection so closing can animate the same content as opening.
  const [sources, setSources] = useState(nextSources ?? []);
  if (nextSources && nextSources !== sources) setSources(nextSources);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus({ preventScroll: true });
    return () => { if (previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented && !document.querySelector('dialog[open]')) { event.preventDefault(); onClose(); }
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [open, onClose]);
  return <aside ref={ref} id="source-panel" className={`source-panel side-panel ${open ? 'open' : ''}`} inert={!open}
    tabIndex={-1} aria-hidden={!open} aria-label={t('来源')}>
    <header><h2>{t('来源')} <span>{sources.length}</span></h2>
      <button type="button" className="icon-button" aria-label={t('关闭来源面板')} onClick={onClose}><X size={20} /></button></header>
    <div className="source-list">{sources.map(source => <a className="source-card" key={source.url} href={source.url}
      target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
      <h3>{source.title}</h3>{source.description && <p>{source.description}</p>}
      <footer><span className="source-site"><SourceIcon source={source} />{source.site}</span><ExternalLink size={14} /></footer>
    </a>)}</div>
  </aside>;
}
