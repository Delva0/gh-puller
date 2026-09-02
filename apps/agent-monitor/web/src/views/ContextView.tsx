/** Render the canonical Model, Context, and correlated activity directly. */

import { useMemo } from 'react';
import { useLanguage } from '@gh-puller/ui';
import { JsonTree, MarkdownText } from '../vendor/dsh';
import type {
  Block,
  Message,
  ModelActivity,
  RequestState,
  ToolActivity,
} from '../events/types';

function jsonValue(value: unknown) {
  if (typeof value === 'object' && value !== null) {
    return <JsonTree data={value as object | unknown[]} copyable expandTopLevel />;
  }
  return <pre className="overflow-x-auto p-2 text-xs">{JSON.stringify(value, null, 2)}</pre>;
}

function ToolCall({ block, tool, committed }: {
  block: Block;
  tool?: ToolActivity;
  committed: boolean;
}) {
  const callId = String(block.callId ?? '');
  const name = String(block.name ?? tool?.name ?? 'tool');
  const status = tool === undefined
    ? 'pending'
    : tool.endSeq === undefined
      ? 'running'
      : tool.error === undefined ? 'completed' : 'error';
  const result = tool?.error ?? tool?.result;
  return (
    <section
      className="my-2 overflow-hidden rounded-md border border-[var(--border-color)] bg-[var(--background)]"
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
          {jsonValue(block.arguments ?? tool?.arguments ?? {})}
        </div>
      </details>
      {!committed && tool?.endSeq !== undefined && (
        <div className="border-t border-[var(--border-color)]">
          {jsonValue(result)}
        </div>
      )}
    </section>
  );
}

function ToolDefinition({ block }: { block: Block }) {
  return (
    <details className="my-2 rounded border border-[var(--border-color)] px-3 py-2 text-xs">
      <summary className="cursor-pointer font-mono">
        tool_definition · {String(block.name ?? '')}
      </summary>
      {typeof block.description === 'string' && (
        <p className="my-2 text-[var(--muted)]">{block.description}</p>
      )}
      <div className="overflow-hidden rounded border border-[var(--border-color)]">
        {jsonValue(block.inputSchema ?? {})}
      </div>
    </details>
  );
}

function ContentBlock({ block, role, tools, committedCalls, streaming = false }: {
  block: Block;
  role: string;
  tools: Map<string, ToolActivity>;
  committedCalls: Set<string>;
  streaming?: boolean;
}) {
  if (block.type === 'text' && typeof block.text === 'string') {
    if (role === 'tool') {
      return <pre className="my-2 overflow-x-auto whitespace-pre-wrap font-mono text-xs">{block.text}</pre>;
    }
    return <MarkdownText text={block.text} streaming={streaming} />;
  }
  if (block.type === 'reasoning' && typeof block.text === 'string') {
    return (
      <details className="my-2 text-xs text-[var(--muted)]">
        <summary className="cursor-pointer select-none">reasoning</summary>
        <div className="mt-2 border-l border-[var(--border-color)] pl-3">
          <MarkdownText text={block.text} streaming={streaming} />
        </div>
      </details>
    );
  }
  if (block.type === 'tool_call') {
    const callId = String(block.callId ?? '');
    return (
      <ToolCall
        block={block}
        tool={tools.get(callId)}
        committed={committedCalls.has(callId)}
      />
    );
  }
  if (block.type === 'tool_definition') return <ToolDefinition block={block} />;
  return (
    <details className="my-2 rounded border border-[var(--border-color)] px-3 py-2 text-xs">
      <summary className="cursor-pointer font-mono">{block.type}</summary>
      <div className="mt-2 overflow-hidden rounded border border-[var(--border-color)]">
        {jsonValue(block)}
      </div>
    </details>
  );
}

function MessageCard({ message, tools, committedCalls }: {
  message: Message;
  tools: Map<string, ToolActivity>;
  committedCalls: Set<string>;
}) {
  const callId = typeof message.callId === 'string' ? message.callId : '';
  const tool = callId === '' ? undefined : tools.get(callId);
  const toolStatus = tool === undefined
    ? 'pending'
    : tool.endSeq === undefined
      ? 'running'
      : tool.error !== undefined || message.isError === true ? 'error' : 'completed';
  const special = message.role === 'system' || message.role === 'developer';
  return (
    <article
      data-context-role={message.role}
      className={`rounded-lg border px-4 py-3 ${
        message.role === 'user'
          ? 'ml-auto max-w-[85%] border-[var(--accent-secondary)] bg-[var(--card-bg)]'
          : special
            ? 'border-dashed border-[var(--border-color)] bg-[var(--background)]'
            : 'border-[var(--border-color)] bg-[var(--card-bg)]'
      }`}
    >
      <div className="mb-2 flex items-center gap-2 text-[10px] uppercase tracking-wide text-[var(--muted)]">
        <span>{message.role}</span>
        {typeof message.name === 'string' && <span>· {message.name}</span>}
        {callId !== '' && <span className="font-mono normal-case">· {callId}</span>}
        {message.role === 'tool' && tool !== undefined && (
          <span className="ml-auto" data-tool-status={toolStatus}>
            {toolStatus}
          </span>
        )}
      </div>
      {message.content.map((block, index) => (
        <ContentBlock
          key={`${block.type}:${index}`}
          block={block}
          role={message.role}
          tools={tools}
          committedCalls={committedCalls}
        />
      ))}
    </article>
  );
}

function LiveAssistant({ activity, tools, committedCalls }: {
  activity: ModelActivity;
  tools: Map<string, ToolActivity>;
  committedCalls: Set<string>;
}) {
  const { t } = useLanguage();
  const blocks: Block[] = [];
  if (activity.reasoning !== '') blocks.push({ type: 'reasoning', text: activity.reasoning });
  if (activity.text !== '') blocks.push({ type: 'text', text: activity.text });
  for (const call of activity.toolCalls.values()) {
    blocks.push({
      type: 'tool_call',
      callId: call.callId,
      name: call.name ?? '',
      arguments: call.arguments,
    });
  }
  return (
    <article
      className="rounded-lg border border-[var(--accent-primary)]/40 bg-[var(--card-bg)] px-4 py-3"
      data-live-assistant
    >
      <div className="mb-2 text-[10px] uppercase tracking-wide text-[var(--muted)]">
        assistant · streaming
      </div>
      {blocks.length === 0 ? (
        <span className="text-xs text-[var(--muted)]">{t('model.waiting')}</span>
      ) : blocks.map((block, index) => (
        <ContentBlock
          key={`${block.type}:${index}`}
          block={block}
          role="assistant"
          tools={tools}
          committedCalls={committedCalls}
          streaming
        />
      ))}
    </article>
  );
}

export default function ContextView({ state, tools, activeModel }: {
  state: RequestState;
  tools: ToolActivity[];
  activeModel: ModelActivity | null;
}) {
  const { t } = useLanguage();
  const toolMap = useMemo(() => new Map(tools.map(tool => [tool.callId, tool])), [tools]);
  const committedCalls = useMemo(() => new Set(
    state.context
      .filter(message => message.role === 'tool' && typeof message.callId === 'string')
      .map(message => String(message.callId)),
  ), [state.context]);

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-3 p-4" data-context-view>
      <section className="rounded-lg border border-[var(--border-color)] bg-[var(--background)] px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wide text-[var(--muted)]">model</span>
          <span className="font-mono text-xs font-medium" data-model-name>
            {state.model === null
              ? 'unset'
              : [state.model.provider, state.model.model].filter(Boolean).join('/')}
          </span>
        </div>
        {state.model !== null && Object.keys(state.model.parameters).length > 0 && (
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer select-none text-[var(--muted)]">
              {t('model.parameters')}
            </summary>
            <div className="mt-2 overflow-hidden rounded border border-[var(--border-color)]">
              {jsonValue(state.model.parameters)}
            </div>
          </details>
        )}
      </section>
      {state.context.map((message, index) => (
        <MessageCard
          key={`${message.role}:${index}`}
          message={message}
          tools={toolMap}
          committedCalls={committedCalls}
        />
      ))}
      {activeModel !== null && (
        <LiveAssistant
          activity={activeModel}
          tools={toolMap}
          committedCalls={committedCalls}
        />
      )}
      {state.context.length === 0 && activeModel === null && (
        <div className="py-8 text-center text-sm text-[var(--muted)]">
          {t('view.noContext')}
        </div>
      )}
    </div>
  );
}
