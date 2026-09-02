/** Render canonical Context Items and correlated activity directly. */

import { useMemo } from 'react';
import { useLanguage } from '@gh-puller/ui';
import { JsonTree, MarkdownText } from '../vendor/dsh';
import type {
  CanonicalState,
  ContentPart,
  Item,
  ModelActivity,
  ToolActivity,
} from '../events/types';

function jsonValue(value: unknown) {
  if (typeof value === 'object' && value !== null) {
    return <JsonTree data={value as object | unknown[]} copyable expandTopLevel />;
  }
  return <pre className="overflow-x-auto whitespace-pre-wrap p-2 text-xs">{String(value ?? '')}</pre>;
}

function argumentsValue(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function ToolCall({ item, tool, committed }: {
  item: Item;
  tool?: ToolActivity;
  committed: boolean;
}) {
  const callId = String(item.call_id ?? '');
  const name = String(item.name ?? tool?.name ?? 'tool');
  const status = tool === undefined
    ? 'pending'
    : tool.endSeq === undefined
      ? 'running'
      : tool.error === undefined ? 'completed' : 'error';
  return (
    <section
      className="my-2 overflow-hidden rounded-md border border-[var(--border-color)] bg-[var(--background)]"
      data-context-item="function_call"
      data-tool-call-id={callId}
    >
      <div className="flex items-center gap-2 border-b border-[var(--border-color)] px-3 py-2">
        <span className="font-mono text-xs font-medium">{name}</span>
        <span className="truncate font-mono text-[10px] text-[var(--muted)]">{callId}</span>
        <span
          className="ml-auto text-[10px] uppercase text-[var(--muted)]"
          data-tool-status={status}
        >
          {status}
        </span>
      </div>
      <details className="px-3 py-2 text-xs">
        <summary className="cursor-pointer select-none text-[var(--muted)]">arguments</summary>
        <div className="mt-2 overflow-hidden rounded border border-[var(--border-color)]">
          {jsonValue(argumentsValue(item.arguments ?? tool?.arguments ?? ''))}
        </div>
      </details>
      {!committed && tool?.endSeq !== undefined && (
        <div className="border-t border-[var(--border-color)]">
          {jsonValue(tool.error ?? tool.result)}
        </div>
      )}
    </section>
  );
}

function FunctionOutput({ item, tool }: { item: Item; tool?: ToolActivity }) {
  const callId = String(item.call_id ?? '');
  const status = tool?.error === undefined ? 'completed' : 'error';
  return (
    <article
      className="rounded-lg border border-[var(--border-color)] bg-[var(--card-bg)]"
      data-context-item="function_call_output"
      data-tool-call-id={callId}
    >
      <div className="flex items-center gap-2 border-b border-[var(--border-color)] px-4 py-2">
        <span className="text-[10px] uppercase tracking-wide text-[var(--muted)]">tool output</span>
        <span className="font-mono text-[10px] text-[var(--muted)]">· {callId}</span>
        <span className="ml-auto text-[10px] uppercase text-[var(--muted)]" data-tool-status={status}>
          {status}
        </span>
      </div>
      {jsonValue(item.output)}
    </article>
  );
}

function ToolDefinition({ part }: { part: ContentPart }) {
  return (
    <details className="my-2 rounded border border-[var(--border-color)] px-3 py-2 text-xs">
      <summary className="cursor-pointer font-mono">
        tool_definition · {String(part.name ?? '')}
      </summary>
      {typeof part.description === 'string' && (
        <p className="my-2 text-[var(--muted)]">{part.description}</p>
      )}
      <div className="overflow-hidden rounded border border-[var(--border-color)]">
        {jsonValue(part.inputSchema ?? {})}
      </div>
    </details>
  );
}

function ContentPartView({ part, streaming = false }: {
  part: ContentPart;
  streaming?: boolean;
}) {
  if (['input_text', 'output_text', 'text'].includes(part.type) && typeof part.text === 'string') {
    return <MarkdownText text={part.text} streaming={streaming} />;
  }
  if (part.type === 'refusal' && typeof part.refusal === 'string') {
    return <MarkdownText text={part.refusal} streaming={streaming} />;
  }
  if (part.type === 'tool_definition') return <ToolDefinition part={part} />;
  return (
    <details className="my-2 rounded border border-[var(--border-color)] px-3 py-2 text-xs">
      <summary className="cursor-pointer font-mono">{part.type}</summary>
      <div className="mt-2 overflow-hidden rounded border border-[var(--border-color)]">
        {jsonValue(part)}
      </div>
    </details>
  );
}

function MessageItem({ item, streaming = false }: { item: Item; streaming?: boolean }) {
  const role = item.role ?? 'unknown';
  const special = role === 'system' || role === 'developer';
  return (
    <article
      data-context-item="message"
      data-context-role={role}
      className={`rounded-lg border px-4 py-3 ${
        role === 'user'
          ? 'ml-auto max-w-[85%] border-[var(--accent-secondary)] bg-[var(--card-bg)]'
          : special
            ? 'border-dashed border-[var(--border-color)] bg-[var(--background)]'
            : 'border-[var(--border-color)] bg-[var(--card-bg)]'
      }`}
    >
      <div className="mb-2 text-[10px] uppercase tracking-wide text-[var(--muted)]">
        {role}
      </div>
      {(item.content ?? []).map((part, index) => (
        <ContentPartView key={`${part.type}:${index}`} part={part} streaming={streaming} />
      ))}
    </article>
  );
}

function ReasoningItem({ item, streaming = false }: { item: Item; streaming?: boolean }) {
  const text = (item.content ?? [])
    .filter(part => part.type === 'reasoning_text' && typeof part.text === 'string')
    .map(part => String(part.text))
    .join('');
  return (
    <details
      className="rounded-lg border border-[var(--border-color)] px-4 py-3 text-xs text-[var(--muted)]"
      data-context-item="reasoning"
    >
      <summary className="cursor-pointer select-none">reasoning</summary>
      <div className="mt-2 border-l border-[var(--border-color)] pl-3">
        {text === '' ? jsonValue(item) : <MarkdownText text={text} streaming={streaming} />}
      </div>
    </details>
  );
}

function ItemView({ item, tools, committedCalls, streaming = false }: {
  item: Item;
  tools: Map<string, ToolActivity>;
  committedCalls: Set<string>;
  streaming?: boolean;
}) {
  if (item.type === 'message') return <MessageItem item={item} streaming={streaming} />;
  if (item.type === 'reasoning') return <ReasoningItem item={item} streaming={streaming} />;
  if (item.type === 'function_call') {
    const callId = String(item.call_id ?? '');
    return <ToolCall item={item} tool={tools.get(callId)} committed={committedCalls.has(callId)} />;
  }
  if (item.type === 'function_call_output') {
    return <FunctionOutput item={item} tool={tools.get(String(item.call_id ?? ''))} />;
  }
  return (
    <details
      className="rounded-lg border border-[var(--border-color)] px-4 py-3 text-xs"
      data-context-item={item.type}
    >
      <summary className="cursor-pointer font-mono">{item.type}</summary>
      <div className="mt-2 overflow-hidden rounded border border-[var(--border-color)]">
        {jsonValue(item)}
      </div>
    </details>
  );
}

function LiveAssistant({ activity, tools, committedCalls }: {
  activity: ModelActivity;
  tools: Map<string, ToolActivity>;
  committedCalls: Set<string>;
}) {
  const { t } = useLanguage();
  const items: Item[] = [];
  if (activity.reasoning !== '') {
    items.push({
      type: 'reasoning',
      content: [{ type: 'reasoning_text', text: activity.reasoning }],
    });
  }
  if (activity.text !== '') {
    items.push({
      type: 'message',
      role: 'assistant',
      content: [{ type: 'output_text', text: activity.text }],
    });
  }
  for (const call of activity.toolCalls.values()) {
    items.push({
      type: 'function_call',
      call_id: call.callId,
      name: call.name ?? '',
      arguments: call.arguments,
    });
  }
  return (
    <section
      className="space-y-3 rounded-lg border border-[var(--accent-primary)]/40 bg-[var(--card-bg)] p-3"
      data-live-assistant
      data-request-id={activity.requestId}
    >
      <div className="text-[10px] uppercase tracking-wide text-[var(--muted)]">
        assistant · streaming · {activity.requestId}
      </div>
      {items.length === 0 ? (
        <span className="text-xs text-[var(--muted)]">{t('model.waiting')}</span>
      ) : items.map((item, index) => (
        <ItemView
          key={`${item.type}:${index}`}
          item={item}
          tools={tools}
          committedCalls={committedCalls}
          streaming
        />
      ))}
    </section>
  );
}

export default function ContextView({ state, tools, activeModels }: {
  state: CanonicalState;
  tools: ToolActivity[];
  activeModels: ModelActivity[];
}) {
  const { t } = useLanguage();
  const toolMap = useMemo(() => new Map(tools.map(tool => [tool.callId, tool])), [tools]);
  const committedCalls = useMemo(() => new Set(
    state.context
      .filter(item => item.type === 'function_call_output' && typeof item.call_id === 'string')
      .map(item => String(item.call_id)),
  ), [state.context]);

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-3 p-4" data-context-view>
      <section className="rounded-lg border border-[var(--border-color)] bg-[var(--background)] px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wide text-[var(--muted)]">agent</span>
          <span className="font-mono text-xs font-medium" data-agent-name>
            {state.agent?.agent ?? 'unset'}
          </span>
        </div>
        {state.agent !== null && Object.keys(state.agent.config).length > 0 && (
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer select-none text-[var(--muted)]">
              {t('agent.config')}
            </summary>
            <div className="mt-2 overflow-hidden rounded border border-[var(--border-color)]">
              {jsonValue(state.agent.config)}
            </div>
          </details>
        )}
      </section>
      {state.context.map((item, index) => (
        <ItemView
          key={`${item.type}:${String(item.call_id ?? item.role ?? index)}:${index}`}
          item={item}
          tools={toolMap}
          committedCalls={committedCalls}
        />
      ))}
      {activeModels.map(activity => (
        <LiveAssistant
          key={activity.requestId}
          activity={activity}
          tools={toolMap}
          committedCalls={committedCalls}
        />
      ))}
      {state.context.length === 0 && activeModels.length === 0 && (
        <div className="py-8 text-center text-sm text-[var(--muted)]">
          {t('view.noContext')}
        </div>
      )}
    </div>
  );
}
