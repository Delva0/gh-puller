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
    activeModels: fold.activeModels(),
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
  it('renders Agent, complete Context, tool correlation, and generic fallbacks', () => {
    const fold = new RunFold();
    fold.applyBatch([
      evt('agent/set', 0, { agent: 'custom', config: { model: 'configured' } }),
      evt('context/set', 1, { items: [{
        type: 'message',
        role: 'system',
        content: [
          { type: 'instruction', text: 'system instruction' },
          { type: 'tool_defs', tools: [
            { name: 'read', inputSchema: { type: 'object' } },
            { name: 'Grep' },
          ] },
          { type: 'mcp', name: 'graph' },
          { type: 'skill_list', skills: ['review'] },
          { type: 'prompt_template', strategy: 'replace' },
        ],
      }] }),
      evt('context/append/user', 2, { items: [{
        type: 'message', role: 'user', content: [{ type: 'input_text', text: 'question' }],
      }] }),
      evt('model/request', 3, { requestId: 'r1', model: 'actual' }),
      evt('model/delta/tool-call', 4, {
        requestId: 'r1', index: 0, callId: 'c1', name: 'read', argumentsDelta: '{"path":"a.py"}',
      }),
      evt('model/response', 5, {
        requestId: 'r1',
        output: [{
          type: 'function_call', call_id: 'c1', name: 'read', arguments: '{"path":"a.py"}',
        }],
      }),
      evt('context/append/assistant', 6, { items: [{
        type: 'function_call', call_id: 'c1', name: 'read', arguments: '{"path":"a.py"}',
      }] }),
      evt('tool/start', 7, { callId: 'c1', name: 'read', arguments: { path: 'a.py' } }),
      evt('tool/end', 8, { callId: 'c1', result: 'file body' }),
      evt('context/append/tool', 9, { items: [{
        type: 'function_call_output', call_id: 'c1', output: 'file body',
      }] }),
      evt('context/append', 10, {
        items: [{ type: 'message', role: 'critic', content: [{ type: 'score', value: 0.9 }] }],
      }),
    ]);
    render(fold);

    expect(container.querySelector('[data-agent-name]')?.textContent).toBe('custom');
    expect(container.textContent).toContain('system instruction');
    expect(container.textContent).toContain('tool_defs · 2');
    expect(container.querySelector('[data-tool-name="read"]')).not.toBeNull();
    expect(container.querySelector('[data-tool-name="Grep"]')).not.toBeNull();
    expect(container.querySelectorAll('[data-tool-schema]')).toHaveLength(1);
    expect(container.querySelector('[data-system-part="mcp"]')?.textContent).toContain('graph');
    expect(container.querySelector('[data-system-part="skill_list"]')).not.toBeNull();
    expect(container.textContent).toContain('prompt_template');
    expect(container.textContent).toContain('replace');
    expect(container.textContent).toContain('question');
    expect(container.textContent).toContain('file body');
    expect(container.querySelector('[data-tool-call-id="c1"]')).not.toBeNull();
    expect(container.querySelector('[data-context-role="critic"]')).not.toBeNull();
    expect(container.textContent).toContain('score');
  });

  it('lets context/set replace the visible model context without presentation metadata', () => {
    const fold = new RunFold();
    fold.applyBatch([
      evt('context/append/user', 0, { items: [{
        type: 'message', role: 'user', content: [{ type: 'input_text', text: 'obsolete' }],
      }] }),
      evt('context/set', 1, {
        items: [{
          type: 'message', role: 'assistant',
          content: [{ type: 'output_text', text: 'summary' }],
        }],
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
      evt('context/append/user', 0, { items: [{
        type: 'message', role: 'user', content: [{ type: 'input_text', text: 'prompt' }],
      }] }),
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
    expect(container.textContent).toContain('state at request');
    expect(container.textContent).toContain('stream deltas (2)');
  });
});
