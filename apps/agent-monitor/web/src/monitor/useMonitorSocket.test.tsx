// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { HubFrame } from './protocol';
import { sessionStore } from './useMonitorSession';
import { useMonitorSocket } from './useMonitorSocket';

class FakeSocket {
  static readonly OPEN = 1;
  static instances: FakeSocket[] = [];

  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  open(): void {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  send(value: string): void {
    this.sent.push(JSON.parse(value) as Record<string, unknown>);
  }

  receive(frame: HubFrame): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  close(): void {
    this.readyState = 3;
  }
}

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
let monitor: ReturnType<typeof useMonitorSocket> | null;

function Harness() {
  monitor = useMonitorSocket();
  return null;
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
  sessionStore.reset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  monitor = null;
  act(() => root.render(<Harness />));
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
  vi.unstubAllGlobals();
});

it('loads every compact history page before publishing one canonical fold', () => {
  const socket = FakeSocket.instances[0];
  act(() => socket.open());
  expect(socket.sent).toEqual([{ type: 'index' }]);

  act(() => socket.receive({
    type: 'index',
    sessions: [{
      session: 's1', label: 'run', state: 'running', ts: 1, last_ts: 2, num_events: 6,
    }],
  }));
  act(() => monitor?.select('s1'));
  expect(socket.sent.at(-1)).toEqual({ type: 'subscribe', session: 's1' });

  act(() => socket.receive({ type: 'evt_ready', session: 's1', lastSeq: 5 }));
  expect(socket.sent.at(-1)).toEqual({
    type: 'history', session: 's1', max: 1000,
  });

  act(() => socket.receive({
    type: 'evt',
    event: {
      seq: 6, ts: 7, session: 's1', type: 'context/append/user',
      data: { content: [{ type: 'text', text: 'next' }] },
    },
  }));
  act(() => socket.receive({
    type: 'history', session: 's1', hasMore: true, nextBeforeSeq: 3,
    events: [
      { seq: 3, ts: 4, session: 's1', type: 'context/append/user', data: {
        content: [{ type: 'text', text: 'question' }],
      } },
      { seq: 4, ts: 5, session: 's1', type: 'model/request', data: { requestId: 'r1' } },
      { seq: 5, ts: 6, session: 's1', type: 'context/append/assistant', data: {
        content: [{ type: 'text', text: 'answer' }],
      } },
    ],
  }));
  expect(socket.sent.at(-1)).toEqual({
    type: 'history', session: 's1', beforeSeq: 3, max: 1000,
  });
  expect(sessionStore.snapshot().loaded).toBe(false);

  act(() => socket.receive({
    type: 'history', session: 's1', hasMore: false, nextBeforeSeq: null,
    events: [
      { seq: 0, ts: 1, session: 's1', type: 'session/start', data: { label: 'run' } },
      { seq: 1, ts: 2, session: 's1', type: 'agent/set', data: {
        agent: 'custom', config: { model: 'configured' },
      } },
      { seq: 2, ts: 3, session: 's1', type: 'context/set', data: {
        messages: [{ role: 'system', content: [{ type: 'text', text: 'instruction' }] }],
      } },
    ],
  }));

  const snapshot = sessionStore.snapshot();
  expect(snapshot.loaded).toBe(true);
  expect(snapshot.events.map(event => event.seq)).toEqual([0, 1, 2, 3, 4, 5, 6]);
  expect(snapshot.state.agent).toEqual({ agent: 'custom', config: { model: 'configured' } });
  expect(snapshot.state.context.map(message => message.role))
    .toEqual(['system', 'user', 'assistant', 'user']);
});
