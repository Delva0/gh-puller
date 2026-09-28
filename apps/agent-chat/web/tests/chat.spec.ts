import { expect, test, type Page, type Download } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const secret = 'fixture-model-secret-value';

async function login(page: Page) {
  await page.goto('/');
  await page.getByLabel('访问口令').fill('test-private-passphrase');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByLabel('输入问题')).toBeVisible();
}
async function configure(page: Page) {
  await page.getByRole('button', { name: '打开设置' }).click();
  await page.getByLabel('模型地址').fill('https://model.example/v1');
  await page.getByLabel('模型 API Key').fill(secret);
  await page.getByRole('tab', { name: '工具', exact: true }).click();
  await page.locator('[name="web_search_backend"]').selectOption('"duckduckgo"');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByLabel('模型名', { exact: true }).fill('fixture-model');
}
async function send(page: Page, prompt: string) {
  await page.getByLabel('输入问题').fill(prompt);
  await page.getByLabel('输入问题').press('Enter');
}
async function downloaded(item: Download) {
  return JSON.parse(await readFile((await item.path())!, 'utf8'));
}
async function captureDownload(page: Page, name: string) {
  const pending = page.waitForEvent('download');
  await page.getByRole('button', { name, exact: true }).click();
  return downloaded(await pending);
}
async function browserRecords(page: Page) {
  return page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('trace-agent-chat');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const names = [...db.objectStoreNames];
    const tx = db.transaction(names);
    const values = await Promise.all(names.map(name => new Promise<unknown>((resolve, reject) => {
      const request = tx.objectStore(name).getAll();
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    })));
    db.close();
    return JSON.stringify(values);
  });
}

test.afterEach(async ({ page }) => {
  await page.request.post('/api/auth/logout', { data: {} });
});

test('credentials, streaming, traces, history management and refresh', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await login(page);
  await expect(page.locator('select[aria-label="选择 agent"] option[value=code]')).toBeDisabled();
  await expect(page.locator('.welcome button')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '打开设置' })).toHaveCount(1);
  await configure(page);
  await page.getByLabel('输入问题').fill('secret evidence');
  await page.getByLabel('输入问题').press('Shift+Enter');
  await expect(page.getByLabel('输入问题')).toHaveValue('secret evidence\n');
  await page.getByLabel('输入问题').press('Enter');
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible();
  await expect(page.locator('.execution > summary')).toContainText('已完成');
  await expect(page.locator('.markdown table')).toBeVisible();
  await expect(page.getByLabel('选择 agent')).toBeDisabled();
  await page.locator('.execution > summary').click();
  await expect(page.locator('.trace-count')).toContainText('2 次模型请求 · 1 次工具调用');
  const tool = page.locator('.trace-item').filter({ hasText: 'github' });
  await tool.locator('summary').click();
  await expect(tool).toContainText('/repos/o/r');
  expect(await page.locator('.trace-item').evaluateAll(items => items.map(item => item.getAttribute('data-kind')))).toEqual(['model', 'tool', 'model']);
  await page.locator('.trace-item[data-kind=model]').first().locator('summary').click();
  await expect(page.locator('.trace-item[data-kind=model]').first().locator('.trace-body pre')).toBeInViewport({ ratio: 1 });
  await expect(page.locator('.trace')).toContainText('先核对原始资料。');
  await expect(page.locator('.trace')).not.toContainText(secret);
  await page.screenshot({ path: info.outputPath('desktop-trace.png'), fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '复制代码' }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('follow_evidence');

  const events = await captureDownload(page, '导出事件');
  expect(JSON.stringify(events)).not.toContain(secret);
  expect(events.events.at(-1).data.status).toBe('completed');
  expect(events.events.map((e: { seq: number }) => e.seq)).toEqual(events.events.map((_: unknown, i: number) => i + 1));
  expect(await browserRecords(page)).not.toContain(secret);
  expect(await browserRecords(page)).not.toContain('test-private-passphrase');

  await page.getByRole('button', { name: '重命名 secret evidence' }).click();
  await page.getByLabel('会话标题').fill('证据笔记');
  await page.getByRole('button', { name: '保存标题' }).click();
  await expect(page.getByRole('dialog', { name: '重命名会话' })).not.toBeVisible();
  await page.getByLabel('搜索历史').fill('没有匹配');
  await expect(page.getByText('没有匹配的会话')).toBeVisible();
  await page.getByLabel('搜索历史').fill('证据');
  await expect(page.locator('.chat-open')).toHaveCount(1);
  await page.getByRole('button', { name: '清空搜索' }).click();

  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByLabel('选择 agent')).toHaveValue('github');
  await page.getByRole('button', { name: '打开设置' }).click();
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await page.locator('[name=backend]').selectOption('"dsl"');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByLabel('选择 agent').selectOption('gitcode');
  await configure(page);
  await send(page, 'GitCode evidence');
  await expect(page.locator('.execution > summary')).toContainText('已完成');
  await page.locator('.chat-open').filter({ hasText: '证据笔记' }).click();
  await expect(page.locator('.user-message')).toHaveText('secret evidence');
  await page.reload();
  await expect(page.locator('.chat-open').filter({ hasText: '证据笔记' })).toBeVisible();
  await page.locator('.chat-open').filter({ hasText: '证据笔记' }).click();
  await page.getByRole('button', { name: '打开设置' }).click();
  await expect(page.getByLabel('模型 API Key')).toHaveValue('');
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await expect(page.getByText(/修改将在新会话生效/)).toBeVisible();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await send(page, 'continue context');
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await expect(page.locator('.markdown').last()).toContainText('已有上下文');

  const history = await captureDownload(page, '导出历史');
  expect(history.conversations).toHaveLength(2);
  expect(JSON.stringify(history)).not.toContain(secret);
  expect(JSON.stringify(history)).not.toContain('server_id');
  await page.getByLabel('导入历史文件').setInputFiles({ name: 'history.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(history)) });
  await expect(page.getByText('此会话为只读历史')).toBeVisible();
  await expect(page.getByLabel('输入问题')).toBeDisabled();
  await expect(page.locator('.chat-open')).toHaveCount(4);
  await page.locator('.chat-row.active').getByRole('button', { name: /^删除/ }).click();
  await page.getByRole('button', { name: '确认删除' }).click();
  await expect(page.locator('.chat-open')).toHaveCount(3);
  expect(errors).toEqual([]);
});

test('disconnect leaves the query running, stop works after refresh, expired history is read-only', async ({ page }) => {
  await login(page);
  await configure(page);
  await send(page, 'slow tool');
  await page.locator('.execution > summary').click();
  await expect(page.locator('.trace-item').filter({ hasText: 'github' })).toContainText('执行中');
  const sessions = await (await page.request.get('/api/sessions')).json();
  const id = sessions[0].id;
  await page.reload();
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible();
  expect((await (await page.request.get(`/api/sessions/${id}`)).json()).running).toBe(true);
  await page.getByRole('button', { name: '停止生成' }).click();
  await expect(page.locator('.execution > summary')).toContainText('已停止');
  await send(page, 'continue after cancel');
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  const data = await captureDownload(page, '导出事件');
  expect(data.events.filter((event: { type: string }) => event.type === 'query/start')).toHaveLength(2);
  expect(new Set(data.events.map((event: { seq: number }) => event.seq)).size).toBe(data.events.length);
  await page.request.delete(`/api/sessions/${id}`, { data: {} });
  await page.reload();
  await expect(page.getByText('此会话为只读历史')).toBeVisible();
  await expect(page.getByLabel('输入问题')).toBeDisabled();
  await expect(page.locator('.markdown').last()).toContainText('找到了可以核对的依据');
});

test('mobile drawer, long histories, code, tables and inert HTML', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await expect(page.getByRole('button', { name: '打开侧栏' })).toBeVisible();
  await page.getByRole('button', { name: '打开侧栏' }).click();
  await expect(page.getByLabel('搜索历史')).toBeVisible();
  const directory = await (await page.request.get('/api/catalog')).json();
  const at = new Date().toISOString();
  const answer = '# 长内容也可以从容阅读\n\n'
    + '<img src=x onerror="window.HTML_EXECUTED=true"><script>window.HTML_EXECUTED=true</script>\n\n'
    + '| 字段 | 来源 | 结果 | 更多信息 |\n|---|---|---|---|\n'
    + '| ' + ['very_long_nonbreaking_reference_'.repeat(5), 'https://example.org', 'success', 'evidence'].join(' | ') + ' |\n\n'
    + '```python\nsource = "' + 'x'.repeat(500) + '"\n```\n\n'
    + '保留足够的留白，让信息容易阅读。\n\n'.repeat(65);
  const conversations = Array.from({ length: 42 }, (_, index) => ({
    title: `研究笔记 ${index + 1}`, agent: 'github', created: at, settings: directory.defaults,
    events: [
      { seq: 1, type: 'query/start', at, query_id: 'imported-question', data: { prompt: '核对长代码与表格的展示' } },
      { seq: 2, type: 'query/end', at, query_id: 'imported-question', data: { status: 'completed', answer, duration_ms: 1300 } },
    ],
  }));
  await page.getByLabel('导入历史文件').setInputFiles({ name: 'long-history.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({ version: 1, conversations })) });
  await expect(page.locator('.chat-open')).toHaveCount(43);
  await page.screenshot({ path: info.outputPath('mobile-drawer.png'), fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '折叠侧栏' }).click();
  await page.locator('.conversation-scroll').evaluate(element => { element.scrollTop = 0; });
  await expect(page.getByRole('button', { name: '回到底部' })).toBeVisible();
  await expect(page.locator('.markdown img, .markdown script')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).HTML_EXECUTED)).toBeUndefined();
  expect(await page.locator('.code-block pre').evaluate(e => e.scrollWidth > e.clientWidth)).toBe(true);
  expect(await page.locator('.table-scroll').evaluate(e => e.scrollWidth > e.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-content.png'), fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '回到底部' }).click();
  await expect(page.getByText('此会话为只读历史')).toBeVisible();
  await expect(page.getByRole('button', { name: '回到底部' })).not.toBeVisible();
});

test('scrolling upward during output preserves the reading position', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await login(page);
  await configure(page);
  await send(page, 'long scroll');
  const view = page.locator('.conversation-scroll');
  await expect.poll(() => view.evaluate(e => e.scrollHeight - e.clientHeight)).toBeGreaterThan(700);
  await view.hover();
  await page.mouse.wheel(0, -10000);
  await expect.poll(() => view.evaluate(e => e.scrollTop)).toBeLessThan(50);
  const before = (await page.locator('.markdown').innerText()).length;
  await expect.poll(async () => (await page.locator('.markdown').innerText()).length).toBeGreaterThan(before + 100);
  expect(await view.evaluate(e => e.scrollTop)).toBeLessThan(50);
  await page.getByRole('button', { name: '回到底部' }).click();
  await expect(page.locator('.execution > summary')).toContainText('已完成');
  await expect.poll(() => view.evaluate(e => e.scrollHeight - e.clientHeight - e.scrollTop)).toBeLessThan(90);
});

test('configuration is discovered, independently saved, and dialog drag does not dismiss', async ({ page }, info) => {
  await login(page);
  await configure(page);
  await page.getByRole('button', { name: '打开设置' }).click();
  await expect(page.getByRole('button', { name: /保存设置|取消/ })).toHaveCount(0);
  const input = await page.getByLabel('模型地址').boundingBox();
  await page.mouse.move(input!.x + 60, input!.y + 15);
  await page.mouse.down();
  await page.mouse.move(5, 5, { steps: 8 });
  await page.mouse.up();
  await expect(page.getByRole('dialog', { name: '设置' })).toBeVisible();
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await expect(page.locator('[name=backend]')).toHaveValue('"rest"');
  await page.locator('[name=ptc]').selectOption('"A"');
  await page.locator('[name=concurrency]').fill('128');
  await page.getByLabel('配置 agent').selectOption('gitcode');
  await expect(page.locator('[name=ptc]')).toHaveValue('false');
  await expect(page.locator('[name=concurrency]')).toHaveValue('8');
  await page.locator('[name=ptc]').selectOption('"B"');
  await page.getByLabel('配置 agent').selectOption('github');
  await expect(page.locator('[name=ptc]')).toHaveValue('"A"');
  await expect(page.locator('[name=concurrency]')).toHaveValue('128');
  await page.getByRole('button', { name: '恢复主包默认配置' }).click();
  await expect(page.locator('[name=ptc]')).toHaveValue('false');
  await page.getByLabel('配置 agent').selectOption('fixture_research');
  await page.locator('[name=strategy]').selectOption('"deep"');
  await page.locator('[name=candidate_count]').fill('1234');
  await page.locator('[name=follow_links]').uncheck();
  await page.screenshot({ path: info.outputPath('desktop-settings.png'), fullPage: true, animations: 'disabled' });
  await page.mouse.click(5, 5);
  await expect(page.getByRole('dialog', { name: '设置' })).not.toBeVisible();
  await page.getByLabel('选择 agent').selectOption('fixture_research');
  await page.getByLabel('思考强度').selectOption('off');
  await send(page, 'discovered agent fields');
  await expect(page.locator('.execution > summary')).toContainText('已完成');
  const record = await captureDownload(page, '导出事件');
  expect(record.session.settings.options).toMatchObject({ strategy: 'deep', candidate_count: 1234, follow_links: false });
  expect(record.events.find((event: { type: string }) => event.type === 'model/request').data.parameters).not.toHaveProperty('reasoning_effort');
  expect(record.events.find((event: { type: string }) => event.type === 'model/request').data.parameters).not.toHaveProperty('max_tokens');
  await page.getByRole('button', { name: '切换为浅色主题' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.screenshot({ path: info.outputPath('desktop-light.png'), fullPage: true, animations: 'disabled' });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', { name: '打开设置' }).click();
  await expect(page.getByLabel('模型 API Key')).toHaveValue('');
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await expect(page.locator('[name=strategy]')).toHaveValue('"deep"');
  await page.getByLabel('配置 agent').selectOption('gitcode');
  await expect(page.locator('[name=ptc]')).toHaveValue('"B"');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByRole('button', { name: '折叠侧栏' }).click();
  await expect(page.locator('.sidebar')).toHaveCSS('visibility', 'hidden');
  await page.getByRole('button', { name: '打开侧栏' }).click();
  await expect(page.locator('.sidebar')).toHaveCSS('opacity', '1');
  expect(await browserRecords(page)).not.toContain(secret);
});

test('legacy flat preferences migrate without rewriting archived settings', async ({ page }) => {
  await login(page);
  const old = { base_url: 'https://legacy.example/v1', model: 'legacy-model', thinking: true, reasoning_effort: 'custom',
    max_tokens: 8192, max_steps: 32, concurrency: 4, backend: 'dsl', ptc: 'B', multimodal: false,
    web_search_backend: 'duckduckgo', web_search_concurrency: 3, web_search_interval: 5 };
  await page.evaluate(async settings => {
    const db = await new Promise<IDBDatabase>(resolve => {
      const open = indexedDB.open('trace-agent-chat'); open.onsuccess = () => resolve(open.result);
    });
    const tx = db.transaction('preferences', 'readwrite');
    tx.objectStore('preferences').put(settings, 'settings');
    await new Promise<void>(resolve => { tx.oncomplete = () => resolve(); }); db.close();
  }, old);
  await page.reload();
  await expect(page.getByLabel('模型名', { exact: true })).toHaveValue('legacy-model');
  await expect(page.getByLabel('思考强度')).toHaveValue('custom');
  await page.getByRole('button', { name: '打开设置' }).click();
  await expect(page.getByLabel('模型地址')).toHaveValue(old.base_url);
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await expect(page.locator('[name=backend]')).toHaveValue('"rest"');
  await expect(page.locator('[name=ptc]')).toHaveValue('false');
  await page.getByRole('tab', { name: '工具', exact: true }).click();
  await expect(page.locator('[name=web_search_concurrency]')).toHaveValue('3');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  const at = new Date().toISOString();
  await page.getByLabel('导入历史文件').setInputFiles({ name: 'old.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify({
    version: 1, conversations: [{ title: '旧版研究', agent: 'github', created: at, settings: old, events: [
      { seq: 1, type: 'query/start', at, query_id: 'old-query', data: { prompt: '旧问题' } },
      { seq: 2, type: 'query/end', at, query_id: 'old-query', data: { answer: '旧答案', status: 'completed', duration_ms: 1 } },
    ] }],
  })) });
  await expect(page.getByText('此会话为只读历史')).toBeVisible();
  const saved = await captureDownload(page, '导出历史');
  expect(saved.conversations.find((chat: { title: string }) => chat.title === '旧版研究').settings).toMatchObject(old);
});
