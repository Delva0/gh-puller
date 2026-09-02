/** Render canonical state, activity, and marker events without a second event model. */

import { useState } from 'react';
import { useLanguage } from '@gh-puller/ui';
import type { EventEnvelope, ModelActivity } from '../events/types';
import { JsonTree } from '../vendor/dsh';

function jsonValue(value: unknown) {
  if (typeof value === 'object' && value !== null) {
    return <JsonTree data={value as object | unknown[]} copyable expandTopLevel />;
  }
  return <pre className="overflow-x-auto p-2 text-xs">{JSON.stringify(value, null, 2)}</pre>;
}

function messagePreview(data: Record<string, unknown>): string {
  const content = Array.isArray(data.content) ? data.content : [];
  const text = content
    .filter(block => typeof block === 'object' && block !== null && 'text' in block)
    .map(block => String((block as { text?: unknown }).text ?? ''))
    .join(' ')
    .trim();
  return text.length > 80 ? `${text.slice(0, 80)}…` : text;
}

function eventSummary(event: EventEnvelope, request?: ModelActivity): string {
  const data = event.data;
  if (event.type === 'session/start') return String(data.label ?? '');
  if (event.type === 'session/end') return String(data.outcome ?? '');
  if (event.type === 'session/error') {
    const error = data.error as { message?: unknown } | undefined;
    return String(error?.message ?? data.scope ?? '');
  }
  if (event.type === 'agent/set') return String(data.agent ?? '');
  if (event.type.startsWith('agent/set/')) {
    const facet = event.type.slice('agent/set/'.length);
    return JSON.stringify(data[facet]);
  }
  if (event.type === 'context/set') {
    return `${Array.isArray(data.messages) ? data.messages.length : 0} messages`;
  }
  if (event.type.startsWith('context/append')) return messagePreview(data);
  if (event.type === 'model/request') {
    const suffix = request === undefined ? '' : ` · ${request.deltaCount} deltas`;
    const target = [data.provider, data.model].filter(Boolean).map(String).join('/');
    return `${String(data.requestId ?? '')}${target ? ` · ${target}` : ''}${suffix}`;
  }
  if (event.type === 'model/response') return String(data.stopReason ?? data.requestId ?? '');
  if (event.type === 'tool/start') return `${String(data.name ?? '')} · ${String(data.callId ?? '')}`;
  if (event.type === 'tool/end') {
    return `${String(data.callId ?? '')} · ${data.error === undefined ? 'completed' : 'error'}`;
  }
  return String(data.outcome ?? data.reason ?? '');
}

function tone(type: string): string {
  if (type.startsWith('agent/')) return 'border-l-violet-400';
  if (type.startsWith('context/')) return 'border-l-emerald-400';
  if (type.startsWith('model/')) return 'border-l-blue-400';
  if (type.startsWith('tool/')) return 'border-l-amber-400';
  if (type.endsWith('/error') || type === 'session/error') return 'border-l-red-400';
  return 'border-l-[var(--border-color)]';
}

function StreamDeltas({ events }: { events: EventEnvelope[] }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  if (events.length === 0) return null;
  return (
    <div className="ml-14 mt-1 text-xs">
      <button
        type="button"
        className="cursor-pointer text-[var(--muted)]"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        {t('event.streamDeltas')} ({events.length})
      </button>
      {open && (
        <div className="mt-1 space-y-1 border-l border-[var(--border-color)] pl-3">
          {events.map(event => (
            <details key={event.seq} className="font-mono text-[11px]">
              <summary className="cursor-pointer select-none">
                #{event.seq} · {event.type}
              </summary>
              <div className="mt-1 overflow-hidden rounded border border-[var(--border-color)]">
                {jsonValue(event.data)}
              </div>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

export default function EventsView({ events, requests }: {
  events: EventEnvelope[];
  requests: ModelActivity[];
}) {
  const { t } = useLanguage();
  const requestsBySeq = new Map(requests.map(request => [request.requestSeq, request]));
  const deltasByRequest = new Map<string, EventEnvelope[]>();
  for (const event of events) {
    if (!event.type.startsWith('model/delta/')) continue;
    const requestId = String(event.data.requestId ?? '');
    const deltas = deltasByRequest.get(requestId) ?? [];
    deltas.push(event);
    deltasByRequest.set(requestId, deltas);
  }
  const visible = events.filter(event => !event.type.startsWith('model/delta/'));

  if (visible.length === 0) {
    return <div className="p-6 text-sm text-[var(--muted)]">{t('view.noEvents')}</div>;
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-2 p-4" data-event-list>
      {visible.map((event) => {
        const request = requestsBySeq.get(event.seq);
        return (
          <article
            key={event.seq}
            data-event-type={event.type}
            className={`border-l-2 bg-[var(--card-bg)] px-3 py-2 ${tone(event.type)}`}
          >
            <div className="flex min-w-0 items-baseline gap-2">
              <span className="w-12 shrink-0 font-mono text-[11px] text-[var(--muted)]">
                #{event.seq}
              </span>
              <span className="shrink-0 font-mono text-xs font-medium">{event.type}</span>
              <span className="truncate text-xs text-[var(--muted)]">
                {eventSummary(event, request)}
              </span>
              <time className="ml-auto shrink-0 font-mono text-[10px] text-[var(--muted)]">
                {new Date(event.ts * 1000).toLocaleTimeString()}
              </time>
            </div>
            <details className="ml-14 mt-1 text-xs">
              <summary className="cursor-pointer select-none text-[var(--muted)]">
                {t('event.payload')}
              </summary>
              <div className="mt-1 overflow-hidden rounded border border-[var(--border-color)]">
                {jsonValue(event.data)}
              </div>
            </details>
            {request !== undefined && (
              <>
                <details className="ml-14 mt-1 text-xs">
                  <summary className="cursor-pointer select-none text-[var(--muted)]">
                    {t('event.stateAtRequest')}
                  </summary>
                  <div className="mt-1 overflow-hidden rounded border border-[var(--border-color)]">
                    {jsonValue(request.stateAtRequest)}
                  </div>
                </details>
                <StreamDeltas events={deltasByRequest.get(request.requestId) ?? []} />
              </>
            )}
          </article>
        );
      })}
    </div>
  );
}
