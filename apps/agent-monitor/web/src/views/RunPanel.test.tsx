// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { LanguageProvider } from '@gh-puller/ui';
import { RunFold } from '../events/fold';
import type { EventEnvelope } from '../events/types';
import { monitorMessages } from '../messages';
import RunPanel from './RunPanel';

function evt(type: string, seq: number, data: Record<string, unknown> = {}): EventEnvelope {
  return { seq, ts: 1700000000 + seq, session: 's1', type, data };
}

function props(fold: RunFold) {
  return {
    loaded: true,
    state: fold.state(),
    events: fold.all(),
    requests: fold.modelActivity(),
    tools: fold.toolActivity(),
    activeModel: fold.activeModel(),
  };
}

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
});

function render(fold: RunFold) {
  act(() => {
    root.render(
      <LanguageProvider extraMessages={monitorMessages}>
        <RunPanel {...props(fold)} />
      </LanguageProvider>,
    );
  });
}

describe('native canonical run view', () => {
  it('renders model, complete context, tool correlation, and generic fallbacks', () => {
    const fold = new RunFold();
    fold.applyBatch([
      evt('model/set', 0, { model: 'm', provider: 'p', parameters: { temperature: 0 } }),
      evt('context/set', 1, { messages: [{
        role: 'system',
        content: [
          { type: 'text', text: 'system instruction' },
          { type: 'tool_definition', name: 'read', inputSchema: { type: 'object' } },
        ],
      }] }),
      evt('context/append/user', 2, { content: [{ type: 'text', text: 'question' }] }),
      evt('model/request', 3, { requestId: 'r1' }),
      evt('model/delta/tool-call', 4, {
        requestId: 'r1', index: 0, callId: 'c1', name: 'read', argumentsDelta: '{"path":"a.py"}',
      }),
      evt('model/response', 5, {
        requestId: 'r1',
        message: { role: 'assistant', content: [{
          type: 'tool_call', callId: 'c1', name: 'read', arguments: { path: 'a.py' },
        }] },
      }),
      evt('context/append/assistant', 6, { content: [{
        type: 'tool_call', callId: 'c1', name: 'read', arguments: { path: 'a.py' },
      }] }),
      evt('tool/start', 7, { callId: 'c1', name: 'read', arguments: { path: 'a.py' } }),
      evt('tool/end', 8, { callId: 'c1', result: 'file body' }),
      evt('context/append/tool', 9, {
        callId: 'c1', name: 'read', content: [{ type: 'text', text: 'file body' }],
      }),
      evt('context/append', 10, {
        role: 'critic', content: [{ type: 'score', value: 0.9 }],
      }),
    ]);
    render(fold);

    expect(container.querySelector('[data-model-name]')?.textContent).toBe('p/m');
    expect(container.textContent).toContain('system instruction');
    expect(container.textContent).toContain('tool_definition · read');
    expect(container.textContent).toContain('question');
    expect(container.textContent).toContain('file body');
    expect(container.querySelector('[data-tool-call-id="c1"]')).not.toBeNull();
    expect(container.querySelector('[data-context-role="critic"]')).not.toBeNull();
    expect(container.textContent).toContain('score');
  });

  it('lets context/set replace the visible model context without presentation metadata', () => {
    const fold = new RunFold();
    fold.applyBatch([
      evt('context/append/user', 0, { content: [{ type: 'text', text: 'obsolete' }] }),
      evt('context/set', 1, {
        messages: [{ role: 'assistant', content: [{ type: 'text', text: 'summary' }] }],
      }),
    ]);
    render(fold);

    expect(container.textContent).not.toContain('obsolete');
    expect(container.textContent).toContain('summary');
    expect(container.querySelectorAll('[data-context-role]')).toHaveLength(1);
  });

  it('shows open model activity and folds stream deltas into one event row', () => {
    const fold = new RunFold();
    fold.applyBatch([
      evt('context/append/user', 0, { content: [{ type: 'text', text: 'prompt' }] }),
      evt('model/request', 1, { requestId: 'r1' }),
      evt('model/delta/reasoning', 2, { requestId: 'r1', index: 0, text: 'think' }),
      evt('model/delta/text', 3, { requestId: 'r1', index: 1, text: 'streaming answer' }),
    ]);
    render(fold);

    expect(container.querySelector('[data-live-assistant]')?.textContent).toContain('streaming answer');
    const eventsButton = [...container.querySelectorAll('button')]
      .find(button => button.textContent === 'Events');
    act(() => eventsButton?.dispatchEvent(new MouseEvent('click', { bubbles: true })));

    expect(container.querySelector('[data-event-type="model/request"]')?.textContent)
      .toContain('2 deltas');
    expect(container.querySelector('[data-event-type="model/delta/text"]')).toBeNull();
    expect(container.textContent).toContain('request state');
    expect(container.textContent).toContain('stream deltas (2)');
  });
});
