// @vitest-environment jsdom
import { createRoot } from 'react-dom/client';
import { act } from 'react';
import { it, expect, beforeEach, afterEach, vi } from 'vitest';
import { LanguageProvider } from '@gh-puller/ui';
import MonitorSessionList from '../MonitorSessionList';
import type { SessionMeta } from '../../monitor-data';

const sessions: SessionMeta[] = [
  { session: 'ns/u1', run_id: 'chat:r1', label: 'chat:r1', provider: 'claude', model: 'm', state: 'running', ts: 1700000000, last_ts: 1700000001, num_events: 3 },
  { session: 'ns/u2', run_id: null, label: 'l2', provider: '', model: '', state: 'completed', ts: 1700000002, last_ts: 1700000003, num_events: 1 },
];

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
let onDelete: ReturnType<typeof vi.fn>;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  onDelete = vi.fn();
  root = createRoot(container);
  act(() => {
    root.render(
      <LanguageProvider>
        <MonitorSessionList sessions={sessions} current={null} onSelect={() => {}} onDelete={onDelete} query="" stateFilter="all" />
      </LanguageProvider>,
    );
  });
});

afterEach(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
});

it('requires explicit confirmation before deleting one session', () => {
  const row = container.querySelectorAll('li')[0];
  const more = row.querySelector('button[aria-haspopup="menu"]')!;
  act(() => {
    more.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  const del = [...document.body.querySelectorAll('button[role="menuitem"]')][0];
  expect(del.textContent).toBe('Delete');
  act(() => {
    del.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });

  const dialog = document.body.querySelector('[role="dialog"]')!;
  expect(dialog.textContent).toContain('Delete session');
  const confirm = [...dialog.querySelectorAll('button')].find((b) => b.textContent === 'Delete')!;
  expect((confirm as HTMLButtonElement).disabled).toBe(true);
  const ack = dialog.querySelector('input[type="checkbox"]')! as HTMLInputElement;
  act(() => {
    ack.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  expect((confirm as HTMLButtonElement).disabled).toBe(false);
  act(() => {
    confirm.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });

  expect(onDelete).toHaveBeenCalledTimes(1);
  expect(onDelete).toHaveBeenCalledWith('ns/u1');
  expect(document.body.querySelector('[role="dialog"]')).toBeNull();
});
