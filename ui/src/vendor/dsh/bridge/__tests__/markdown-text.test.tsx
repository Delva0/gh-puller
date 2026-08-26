// @vitest-environment jsdom
import { createRoot } from 'react-dom/client';
import { act } from 'react';
import { it, expect } from 'vitest';
import { MarkdownText } from '../../ui-primitives/src/markdown/MarkdownText';

it('MarkdownText 渲染纯文本', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<MarkdownText text="你好世界" codeLabels={{ copyLabel: 'c', copiedLabel: 'd' }} />);
    await new Promise((r) => setTimeout(r, 20));
  });
  console.log('[md-debug] body:', JSON.stringify(container.textContent));
  expect(container.textContent).toContain('你好世界');
  act(() => root.unmount());
});
