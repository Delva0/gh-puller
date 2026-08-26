// @vitest-environment jsdom
// 面板宿主渲染冒烟(回归):seed 一个会话 → 渲染 DshConversationPanel →
// 断言 dsh 面板 DOM/对话流出现。禁真机:纯夹具,无网络无 LLM。
import { createRoot } from 'react-dom/client';
import { act } from 'react';
import { it, expect, beforeAll, afterAll } from 'vitest';
import { sessionStore } from '../../../../hooks/useMonitorSession';
import DshConversationPanel from '../DshPanels';
import { LanguageProvider } from '../../../../contexts/LanguageContext';
import type { EventEnvelope } from '../../../../monitor/types';

function evt(type: string, seq: number, data: Record<string, unknown>): EventEnvelope {
  return { id: `e${seq}`, seq, ts: 1700000000.5, session: 'smoke/u1', type, data };
}

let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

beforeAll(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  sessionStore.reset({ sessionId: 'smoke/u1', label: 'smoke:demo', runId: 'smoke:demo', state: 'running' });
  sessionStore.applyBatch([
    evt('session/start', 0, { run_id: 'smoke:demo' }),
    evt('turn/start', 1, { turn: 1 }),
    evt('step/start', 2, { turn: 1, step: 1 }),
    evt('user/message', 3, { turn: 1, step: 1, message: { role: 'user', content: [{ type: 'text', text: '冒烟问题' }] }, source: { kind: 'user' }, surfaceOp: 'append' }),
    evt('assistant/message', 4, { turn: 1, step: 1, message: { role: 'assistant', content: [{ type: 'text', text: '你好世界' }] }, surfaceOp: 'append', usage: { input: 5, output: 6 } }),
    evt('step/end', 5, { turn: 1, step: 1 }),
    evt('turn/end', 6, { turn: 1 }),
  ]);
});

afterAll(() => {
  act(() => root.unmount());
  document.body.removeChild(container);
});

it('渲染出 dsh 对话面板(root 壳 + 会话体 + 消息内容)', async () => {
  let threw: unknown = null;
  root = createRoot(container);
  await act(async () => {
    try {
      root.render(<LanguageProvider><DshConversationPanel /></LanguageProvider>);
    } catch (e) {
      threw = e;
    }
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 50));
  });
  expect(threw).toBeNull();
  expect(container.querySelector('[data-conversation-scroll]')).not.toBeNull();
  expect(container.textContent).toContain('冒烟问题');
  expect(container.textContent).toContain('你好世界');
});
