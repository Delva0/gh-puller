/** Switch between the current Context and canonical event sequence. */

import { useState } from 'react';
import { useLanguage } from '@gh-puller/ui';
import type {
  EventEnvelope,
  ModelActivity,
  RequestState,
  ToolActivity,
} from '../events/types';
import { Pill } from '../vendor/dsh';
import ContextView from './ContextView';
import EventsView from './EventsView';

type View = 'context' | 'events';

export interface RunPanelProps {
  loaded: boolean;
  state: RequestState;
  events: EventEnvelope[];
  requests: ModelActivity[];
  tools: ToolActivity[];
  activeModel: ModelActivity | null;
}

export default function RunPanel({
  loaded,
  state,
  events,
  requests,
  tools,
  activeModel,
}: RunPanelProps) {
  const { t } = useLanguage();
  const [view, setView] = useState<View>('context');

  return (
    <section className="flex h-full min-h-0 flex-col" data-agent-run-panel>
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-[var(--border-color)] px-4">
        <Pill active={view === 'context'} onClick={() => setView('context')}>
          {t('view.context')}
        </Pill>
        <Pill active={view === 'events'} onClick={() => setView('events')}>
          {t('view.events')}
        </Pill>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {!loaded ? (
          <div className="p-6 text-sm text-[var(--muted)]">{t('view.loading')}</div>
        ) : view === 'context' ? (
          <ContextView state={state} tools={tools} activeModel={activeModel} />
        ) : (
          <EventsView events={events} requests={requests} />
        )}
      </div>
    </section>
  );
}
