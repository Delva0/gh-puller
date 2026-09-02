// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterAll, beforeAll, expect, it } from 'vitest'
import { sessionStore } from '../../../../hooks/useMonitorSession'
import type { EventEnvelope } from '../../../../monitor-data/types'
import DshConversationPanel from '../DshPanels'

function evt(type: string, seq: number, data: Record<string, unknown> = {}): EventEnvelope {
  return { seq, ts: 1700000000.5, session: 'smoke/u1', type, data }
}

let container: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeAll(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  sessionStore.reset({
    sessionId: 'smoke/u1', label: 'smoke:demo', runId: 'smoke:demo', state: 'running',
  })
  sessionStore.applyBatch([
    evt('session/start', 0, { runId: 'smoke:demo' }),
    evt('turn/start', 1),
    evt('step/start', 2),
    evt('context/append/user', 3, { content: [{ type: 'text', text: '冒烟问题' }] }),
    evt('context/append/assistant', 4, { content: [{ type: 'text', text: '你好世界' }] }),
    evt('step/end', 5),
    evt('turn/end', 6, { outcome: 'completed' }),
  ])
})

afterAll(() => {
  act(() => root.unmount())
  document.body.removeChild(container)
})

it('renders canonical context with the existing conversation UI', async () => {
  root = createRoot(container)
  await act(async () => {
    root.render(<DshConversationPanel />)
    await new Promise(resolve => setTimeout(resolve, 50))
  })
  expect(container.querySelector('[data-conversation-scroll]')).not.toBeNull()
  expect(container.textContent).toContain('冒烟问题')
  expect(container.textContent).toContain('你好世界')
})

it('renders canonical tool activity through the thin adapter', async () => {
  act(() => sessionStore.reset({
    sessionId: 'smoke/u2', label: 'smoke:tool', runId: 'smoke:tool', state: 'running',
  }))
  sessionStore.applyBatch([
    evt('session/start', 0, { runId: 'smoke:tool' }),
    evt('turn/start', 1),
    evt('step/start', 2),
    evt('context/append/user', 3, { content: [{ type: 'text', text: '跑个工具' }] }),
    evt('context/append/assistant', 4, {
      content: [
        { type: 'reasoning', text: '用 Bash 看看' },
        { type: 'tool_call', callId: 'call_1', name: 'Bash', arguments: { command: 'ls' } },
      ],
    }),
    evt('tool/start', 5, { callId: 'call_1', name: 'Bash', arguments: { command: 'ls' } }),
    evt('tool/end', 6, { callId: 'call_1', result: 'total 0' }),
    evt('context/append/tool', 7, {
      callId: 'call_1', content: [{ type: 'text', text: 'total 0' }],
    }),
    evt('step/end', 8),
    evt('turn/end', 9, { outcome: 'completed' }),
  ])
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 50))
  })
  expect(container.querySelector('[data-chat-call-id]')).not.toBeNull()
  expect(container.textContent).toContain('Bash')
  expect(container.textContent).toContain('用 Bash 看看')
})

it('clears the chat surface after an empty context replacement', async () => {
  act(() => sessionStore.reset({
    sessionId: 'smoke/u3', label: 'smoke:reset', runId: 'smoke:reset', state: 'running',
  }))
  sessionStore.applyBatch([
    evt('session/start', 0, { runId: 'smoke:reset' }),
    evt('context/append/user', 1, { content: [{ type: 'text', text: 'obsolete-context' }] }),
  ])
  sessionStore.ingestBatch([evt('context/set', 2, { messages: [] })])
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 50))
  })
  expect(container.textContent).not.toContain('obsolete-context')
})
