import { expect, test, type Page, type Download } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const secret = 'fixture-model-secret-value';

async function login(page: Page) {
  await page.goto('/');
  await page.getByLabel('访问口令').fill('test-private-passphrase');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByLabel('输入问题')).toBeVisible();
}
async function choose(page: Page, label: string, value: string) {
  await page.getByRole('combobox', { name: label, exact: true }).click();
  await page.getByRole('option', { name: new RegExp('^' + value + '$', 'i') }).click();
}
async function selectTool(page: Page, id: string) {
  await page.getByRole('tab', { name: '工具', exact: true }).click();
  await page.locator(`#settings-tools .tool-node[data-tool=${id}]`).click();
}
async function configure(page: Page) {
  await page.getByRole('button', { name: '打开设置' }).click();
  await page.getByLabel('模型地址').fill('https://model.example/v1');
  await page.getByLabel('模型 API Key').fill(secret);
  await selectTool(page, 'web_search');
  await page.locator('[name="web_search_backend"]').selectOption('"duckduckgo"');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  if (page.viewportSize()!.width <= 760) await page.getByRole('button', { name: '折叠侧栏' }).click();
  await choose(page, '模型名', 'fixture-model');
}
async function send(page: Page, prompt: string) {
  await page.getByLabel('输入问题').fill(prompt);
  await expect(page.getByRole('button', { name: '发送问题', exact: true })).toBeEnabled();
  await page.getByLabel('输入问题').press('Enter');
  const confirmation = page.getByRole('dialog', { name: '继续会话', exact: true });
  await expect.poll(async () => await confirmation.isVisible() || await page.getByRole('button', { name: '停止生成' }).isVisible()).toBe(true);
  if (await confirmation.isVisible()) {
    await confirmation.getByLabel('本会话不再提示').check();
    await confirmation.getByRole('button', { name: '重建并继续', exact: true }).click();
    await expect(confirmation).not.toBeVisible();
  }
}
async function downloaded(item: Download) {
  return JSON.parse(await readFile((await item.path())!, 'utf8'));
}
async function captureDownload(page: Page, name: string) {
  const pending = page.waitForEvent('download');
  await page.getByRole('button', { name, exact: true }).click();
  const file = await pending;
  const contents = await downloaded(file);
  if (name === '导出事件') expect(file.suggestedFilename()).toBe(`events_${contents.session.id}.json`);
  return contents;
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
  await expect(page.locator('.chat-row')).toHaveCount(0);
  await page.getByLabel('选择 agent').click();
  await expect(page.getByRole('option', { name: /^Code/ })).toBeDisabled();
  await page.keyboard.press('Escape');
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
  await expect(page.getByLabel('选择 agent')).toBeEnabled();
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
  await expect(page.locator('.chat-row')).toHaveCount(1);
  await expect(page.locator('.chat-row.active')).toHaveCount(0);
  await expect(page.locator('.welcome')).toBeVisible();
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.locator('.chat-row')).toHaveCount(1);
  await expect(page.getByLabel('选择 agent')).toHaveText('GitHub');
  await page.getByRole('button', { name: '打开设置' }).click();
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await page.locator('[name=backend]').first().selectOption('"rest"');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await choose(page, '选择 agent', 'GitCode');
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
  await expect(page.getByText(/修改将在新会话生效/)).toHaveCount(0);
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
  await expect(page.getByText('此会话为只读历史')).toHaveCount(0);
  await expect(page.getByLabel('输入问题')).toBeEnabled();
  await expect(page.locator('.chat-open')).toHaveCount(4);
  await page.locator('.chat-row.active').getByRole('button', { name: /^删除/ }).click();
  await page.getByRole('button', { name: '确认删除' }).click();
  await expect(page.locator('.chat-open')).toHaveCount(3);
  expect(errors).toEqual([]);
});

test('disconnect leaves the query running, stop works after refresh, expired history can resume', async ({ page }) => {
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
  await expect(page.locator('.turn-notice')).toHaveCount(0);
  await send(page, 'continue after cancel');
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  const data = await captureDownload(page, '导出事件');
  expect(data.events.filter((event: { type: string }) => event.type === 'query/start')).toHaveLength(2);
  expect(new Set(data.events.map((event: { seq: number }) => event.seq)).size).toBe(data.events.length);
  const current = (await (await page.request.get('/api/sessions')).json())[0].id;
  await page.request.delete(`/api/sessions/${current}`, { data: {} });
  await page.reload();
  await expect(page.getByText('此会话为只读历史')).toHaveCount(0);
  await expect(page.getByLabel('输入问题')).toBeEnabled();
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
  await expect(page.locator('.chat-open')).toHaveCount(42);
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
  await expect(page.getByText('此会话为只读历史')).toHaveCount(0);
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

test('configuration discovery, fixed panels, ranges, keys, import/export and language', async ({ page }, info) => {
  await login(page); await configure(page);
  await page.getByRole('button', { name: '打开设置' }).click();
  await expect(page.getByRole('button', { name: /保存设置|取消/ })).toHaveCount(0);
  const bounds = await page.getByRole('dialog').boundingBox();
  const input = await page.getByLabel('模型地址').boundingBox();
  await page.mouse.move(input!.x + 60, input!.y + 15); await page.mouse.down();
  await page.mouse.move(5, 5, { steps: 8 }); await page.mouse.up();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '显示密码' }).click();
  await expect(page.getByLabel('模型 API Key')).toHaveAttribute('type', 'text');
  await page.getByRole('button', { name: '隐藏密码' }).click();
  await page.getByRole('button', { name: '测试连接' }).click();
  await expect(page.getByRole('tabpanel')).toContainText('连接成功');
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  expect((await page.getByRole('dialog').boundingBox())!.height).toBe(bounds!.height);
  await expect(page.locator('[name=backend]').first()).toHaveValue('"rest"');
  await page.locator('[name=ptc]').selectOption('"A"');
  await page.locator('[name=concurrency]').first().fill('128');
  await expect(page.locator('[name=concurrency]').first()).toHaveAttribute('aria-invalid', 'true');
  await page.locator('[name=concurrency]').first().blur();
  await expect(page.locator('[name=concurrency]').first()).toHaveValue('8');
  await page.locator('[name=concurrency]').first().fill('16');
  await page.locator('[data-agent=gitcode]').click();
  await page.locator('[name=ptc]').selectOption('"B"');
  await page.locator('[data-agent=fixture_research]').click();
  await page.locator('[name=strategy]').selectOption('"deep"');
  await page.locator('[name=candidate_count]').fill('1234');
  await page.locator('[name=follow_links]').uncheck();
  await page.screenshot({ path: info.outputPath('desktop-settings-agent.png'), fullPage: true, animations: 'disabled' });
  await selectTool(page, 'web_search');
  expect((await page.getByRole('dialog').boundingBox())!.height).toBe(bounds!.height);
  await expect(page.locator('[name=brave_api_key]')).toHaveCount(0);
  await page.locator('[name=web_search_backend]').selectOption('"brave"');
  await expect(page.locator('[name=brave_api_key]')).toBeVisible();
  await page.locator('[name=web_search_backend]').selectOption('"duckduckgo"');
  await expect(page.locator('[name=tool_result_num_user_query]')).toHaveValue('1');
  await expect(page.locator('[name=tool_result_preview_chars]')).toHaveValue('2000');
  const config = await captureDownload(page, '导出配置');
  expect(JSON.stringify(config)).not.toContain(secret);
  await page.getByRole('button', { name: '恢复默认配置' }).click();
  await page.locator('input[type=file][aria-label="导入配置"]').setInputFiles({ name: 'config.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(config)) });
  await expect(page.getByText('配置已导入，密钥不会从文件读取。')).toBeVisible();
  await page.getByRole('tab', { name: '模型', exact: true }).click();
  await page.getByLabel('模型 API Key').fill(secret);
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await choose(page, '选择 agent', 'Research');
  await choose(page, '思考强度', '关闭思考');
  await send(page, 'discovered agent fields');
  await expect(page.locator('.execution > summary')).toContainText('已完成');
  const record = await captureDownload(page, '导出事件');
  expect(record.session.settings.options).toMatchObject({ strategy: 'deep', candidate_count: 1234, follow_links: false });
  expect(record.events.find((event: { type: string }) => event.type === 'model/request').data.parameters).not.toHaveProperty('reasoning_effort');
  await page.getByRole('button', { name: '切换为浅色主题' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.screenshot({ path: info.outputPath('desktop-light.png'), fullPage: true, animations: 'disabled' });
  const resizer = await page.getByRole('separator').boundingBox();
  await page.mouse.move(resizer!.x + 4, resizer!.y + 250); await page.mouse.down();
  await page.mouse.move(355, 300, { steps: 8 }); await page.mouse.up();
  await expect(page.locator('.sidebar')).toHaveCSS('width', '355px');
  await page.reload();
  await expect(page.getByLabel('选择 agent')).toHaveText('Research');
  await expect(page.getByLabel('思考强度')).toHaveText('关闭思考');
  await expect(page.locator('.sidebar')).toHaveCSS('width', '355px');
  await page.getByRole('button', { name: '打开设置' }).click();
  await expect(page.getByLabel('模型 API Key')).toHaveValue('');
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await expect(page.locator('[name=strategy]')).toHaveValue('"deep"');
  await page.locator('[data-agent=gitcode]').click();
  await expect(page.locator('[name=ptc]')).toHaveValue('"B"');
  await page.getByRole('tab', { name: '模型', exact: true }).click();
  await page.getByLabel('语言').selectOption('en');
  await expect(page.getByRole('dialog', { name: 'Settings' })).toBeVisible();
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByRole('button', { name: 'Collapse sidebar' }).click();
  await expect(page.locator('.sidebar')).toHaveCSS('visibility', 'hidden');
  await page.getByRole('button', { name: 'Open sidebar' }).click();
  await expect(page.locator('.sidebar')).toHaveCSS('opacity', '1');
  await page.reload();
  await expect(page.getByRole('button', { name: 'New chat' })).toBeVisible();
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
  await expect(page.getByLabel('模型名', { exact: true })).toHaveText('legacy-model');
  await expect(page.getByLabel('思考强度')).toHaveText('custom');
  await page.getByRole('button', { name: '打开设置' }).click();
  await expect(page.getByLabel('模型地址')).toHaveValue(old.base_url);
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await expect(page.locator('[name=backend]').first()).toHaveValue('"rest"');
  await expect(page.locator('[name=ptc]').first()).toHaveValue('false');
  await selectTool(page, 'web_search');
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
  await expect(page.getByText('此会话为只读历史')).toHaveCount(0);
  const saved = await captureDownload(page, '导出历史');
  expect(saved.conversations.find((chat: { title: string }) => chat.title === '旧版研究').settings).toMatchObject(old);
});

test('model popup, branch edits, regeneration, agent switching and portable history', async ({ page }, info) => {
  await login(page); await configure(page);
  const model = page.getByRole('combobox', { name: '模型名', exact: true });
  const width = (await model.boundingBox())!.width;
  await choose(page, '模型名', 'provider/very-long-model-name-for-research-2026-09-preview');
  expect((await model.boundingBox())!.width).toBe(width);
  await model.click();
  await expect(page.getByRole('option', { name: 'provider/very-long-model-name-for-research-2026-09-preview', exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('model-popup.png'), fullPage: true, animations: 'disabled' });
  await page.keyboard.press('Escape');
  await choose(page, '思考强度', 'Max');
  await send(page, 'first evidence');
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await send(page, 'second evidence');
  await expect(page.locator('.execution > summary')).toHaveCount(2);
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await page.getByRole('button', { name: '编辑问题', exact: true }).last().click();
  await page.getByLabel('编辑消息').fill('edited second evidence');
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight)).toBe(true);
  await page.screenshot({ path: info.outputPath('inline-edit.png'), fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const rebuilding = page.getByRole('dialog', { name: '继续会话', exact: true });
  await rebuilding.getByLabel('本会话不再提示').check();
  await rebuilding.getByRole('button', { name: '重建并继续', exact: true }).click();
  await expect(page.locator('.execution > summary')).toHaveCount(2);
  await expect(page.getByRole('button', { name: '停止生成' })).not.toBeVisible();
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await expect(page.locator('.user-message').last()).toHaveText('edited second evidence');
  await expect(page.locator('.version-control')).toHaveText('2 / 2');
  await expect(page.locator('.execution > summary')).toHaveCount(2);
  await page.getByRole('button', { name: '上一版本', exact: true }).click();
  await expect(page.locator('.user-message').last()).toHaveText('second evidence');
  await expect(page.locator('.version-control')).toHaveText('1 / 2');
  await page.getByRole('button', { name: '下一版本', exact: true }).click();
  await page.getByRole('button', { name: '重新生成', exact: true }).last().click();
  await expect(page.locator('.execution > summary')).toHaveCount(2);
  await expect(page.getByRole('button', { name: '停止生成' })).not.toBeVisible();
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await expect(page.locator('.version-control')).toHaveText('3 / 3');
  const before = await captureDownload(page, '导出事件');
  await choose(page, '选择 agent', 'Web');
  await choose(page, '选择 agent', 'GitHub');
  const after = await captureDownload(page, '导出事件');
  expect(after.events).toEqual(before.events);
  await choose(page, '选择 agent', 'Web');
  await send(page, 'continue with web');
  await expect(page.locator('.execution > summary')).toHaveCount(3);
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await expect(page.locator('.markdown').last()).toContainText('已有上下文');
  await page.locator('.execution > summary').last().click();
  await expect(page.locator('.trace-item[data-kind=tool]').last().locator('summary')).toContainText('读取网页');
  await expect(page.locator('.trace-item[data-kind=tool]').last().locator('.trace-description')).toContainText('web_fetch(requests=[{url=');
  const saved = await captureDownload(page, '导出事件');
  expect(saved.events.some((event: { type: string }) => event.type.startsWith('context/append'))).toBe(true);
  expect(saved.events.some((event: { type: string }) => event.type === 'artifact/saved')).toBe(true);
  expect(saved.events.some((event: { type: string }) => event.type === 'context/checkpoint')).toBe(false);
  expect(saved.events.slice(0, before.events.length)).toEqual(before.events);
  await page.reload();
  await expect(model).toHaveText('provider/very-long-model-name-for-research-2026-09-preview');
  await expect(page.getByLabel('思考强度')).toHaveText('Max');
  await expect(page.getByLabel('选择 agent')).toHaveText('Web');
  await expect(page.locator('.version-control')).toHaveText('3 / 3');
  const id = (await (await page.request.get('/api/sessions')).json())[0].id;
  await page.request.delete(`/api/sessions/${id}`, { data: {} });
  await page.reload();
  await expect(page.getByLabel('输入问题')).toBeEnabled();
  await configure(page);
  await send(page, 'resume after server cleanup');
  await expect(page.locator('.execution > summary')).toHaveCount(4);
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await expect(page.locator('.markdown').last()).toContainText('已有上下文');
  await page.getByLabel('导入历史文件').setInputFiles({ name: 'events.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(saved)) });
  await expect(page.locator('.execution > summary')).toHaveCount(3);
  await expect(page.locator('.version-control')).toHaveText('3 / 3');
  await send(page, 'continue imported checkpoint');
  await expect(page.locator('.execution > summary')).toHaveCount(4);
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  expect(await browserRecords(page)).not.toContain(secret);
  const sessions = (await (await page.request.get('/api/sessions')).json()).length;
  await page.getByRole('button', { name: '上一版本', exact: true }).click();
  await page.locator('.chat-row.active').getByRole('button', { name: /^删除/ }).click();
  await page.getByRole('button', { name: '确认删除' }).click();
  await expect.poll(async () => (await (await page.request.get('/api/sessions')).json()).length).toBe(sessions - 1);
});

test('mobile settings scroll independently and compact pickers fit the viewport', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.getByRole('button', { name: '打开侧栏' }).click();
  await configure(page);
  await page.getByRole('button', { name: '打开侧栏' }).click();
  await page.getByRole('button', { name: '打开设置' }).click();
  const bounds = await page.getByRole('dialog').boundingBox();
  await page.getByRole('tab', { name: '工具', exact: true }).click();
  await page.locator('[name=github_token]').fill('tool-test-key');
  await page.getByRole('button', { name: '测试连接 GitHub API Key', exact: true }).click();
  await expect(page.getByRole('tabpanel')).toContainText('连接成功');
  await page.locator('[name=tool_result_preview_chars]').scrollIntoViewIfNeeded();
  await page.locator('[data-config=tool_result_preview_chars] .config-consumers > button').click();
  const sharedPopup = await page.locator('.consumer-list').boundingBox();
  expect(sharedPopup!.x).toBeGreaterThanOrEqual(0);
  expect(sharedPopup!.x + sharedPopup!.width).toBeLessThanOrEqual(390);
  expect((await page.getByRole('dialog').boundingBox())!.height).toBe(bounds!.height);
  expect(await page.getByRole('dialog').evaluate(element => element.scrollTop)).toBe(0);
  await page.screenshot({ path: info.outputPath('mobile-settings.png'), fullPage: true, animations: 'disabled' });
  await page.locator('[data-config=tool_result_preview_chars] .config-consumers > button').press('Escape');
  await expect(page.locator('.consumer-list')).toHaveCount(0);
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByRole('button', { name: '折叠侧栏' }).click();
  await page.getByLabel('模型名', { exact: true }).click();
  await expect(page.getByRole('option', { name: 'fixture-model', exact: true })).toBeVisible();
  const popup = await page.locator('.select-popup').boundingBox();
  expect(popup!.x).toBeGreaterThanOrEqual(0);
  expect(popup!.x + popup!.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath('mobile-models.png'), fullPage: true, animations: 'disabled' });
});

test('required tool credentials control agent availability, including retained session keys', async ({ page }, info) => {
  await login(page);
  await page.getByLabel('选择 agent').click();
  for (const name of ['GitHub', 'GitCode', 'Web', 'Code']) await expect(page.getByRole('option', { name: new RegExp('^' + name + '\\b') })).toBeDisabled();
  await expect(page.getByRole('option', { name: /^Web\b/ })).toContainText('Brave API Key');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '打开设置' }).click();
  await page.getByLabel('模型地址').fill('https://model.example/v1');
  await page.getByLabel('模型 API Key').fill(secret);
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  const agent = page.locator('section').filter({ has: page.getByRole('heading', { name: 'GitHub', exact: true }) });
  await expect(agent.getByRole('img', { name: '不可用', exact: true })).toHaveAttribute('title', /Brave API Key/);
  await expect(agent.locator('.config-field').first().locator('[title]')).toHaveAttribute('title', '此 Agent 向各工具提供的并发预算。');
  await expect(agent.locator('.config-field small')).toHaveCount(0);
  await page.getByRole('tab', { name: '工具', exact: true }).click();
  const web = page.locator('.tool-node[data-tool=web_search]');
  await expect(page.locator('.tool-node[data-tool=github_rest]')).toBeVisible();
  await expect(page.locator('.tool-node[data-tool=web], .tool-node[data-tool=tool_results]')).toHaveCount(0);
  await expect(web.getByRole('img', { name: '不可用', exact: true })).toBeVisible();
  await expect(page.locator('.tool-node[data-tool=web_fetch]').getByRole('img', { name: '可用', exact: true })).toBeVisible();
  await web.click();
  await page.locator('[name=web_search_backend]').selectOption('"auto"');
  await expect(web.getByRole('img', { name: '可用', exact: true })).toBeVisible();
  await page.locator('[name=web_search_backend]').selectOption('"brave"');
  await page.locator('[name=brave_api_key]').fill('invalid-tool-key');
  await expect(web.getByRole('img', { name: '待校验', exact: true })).toBeVisible();
  await page.locator('[name=brave_api_key]').blur();
  await expect(page.locator('[data-config=brave_api_key]')).toContainText('API Key 验证失败');
  await expect(web.getByRole('img', { name: '不可用', exact: true })).toBeVisible();
  await page.locator('[name=brave_api_key]').fill('fixture-brave-secret');
  await expect(web.getByRole('img', { name: '待校验', exact: true })).toBeVisible();
  await page.locator('[name=web_search_backend]').focus();
  await expect(page.locator('[data-config=brave_api_key]')).toContainText('连接成功');
  await expect(web.getByRole('img', { name: '可用', exact: true })).toBeVisible();
  await expect(page.locator('[name=web_search_backend]')).toHaveCSS('outline-style', 'none');
  await page.screenshot({ path: info.outputPath('settings-tool-availability.png'), fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await choose(page, '模型名', 'fixture-model');
  await send(page, 'retain required credentials');
  await expect(page.locator('.execution > summary')).toContainText('已完成');
  const queryFont = await page.locator('.user-message > div').evaluate(e => getComputedStyle(e).fontSize);
  await expect(page.locator('.markdown')).toHaveCSS('font-size', queryFont);
  await page.reload();
  await page.getByLabel('选择 agent').click();
  await expect(page.getByRole('option', { name: 'Web', exact: true })).toBeEnabled();
  await page.keyboard.press('Escape');
  await send(page, 'continue with retained keys');
  await expect(page.locator('.execution > summary')).toHaveCount(2);
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.locator('.chat-row.active')).toHaveCount(0);
  await expect(page.locator('.chat-row')).toHaveCount(1);
  await page.getByLabel('选择 agent').click();
  await expect(page.getByRole('option', { name: /^Web\b/ })).toBeDisabled();
  await page.keyboard.press('Escape');
  expect(await browserRecords(page)).not.toContain('fixture-brave-secret');
});

test('native tool identities, agent tiles and shared field navigation follow package choices', async ({ page }, info) => {
  await login(page); await configure(page);
  await page.getByRole('button', { name: '打开设置' }).click();
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await expect(page.locator('.agent-index [aria-pressed=true]')).toHaveCount(1);
  await expect(page.locator('.agent-config')).toHaveCount(1);
  await expect(page.locator('.agent-tools [data-tool=github_rest]')).toBeVisible();
  await page.locator('[name=backend]').selectOption('"dsl"');
  await expect(page.locator('.agent-tools [data-tool=github_dsl]')).toBeVisible();
  await expect(page.locator('.agent-tools [data-tool=github_rest]')).toHaveCount(0);
  await page.locator('.agent-tools [data-tool=github_dsl]').click();
  await expect(page.getByRole('tab', { name: '工具', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#settings-tools [data-tool=github_dsl]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('[data-tool=github]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '全部配置', exact: true })).toHaveCount(0);
  await page.getByRole('tab', { name: 'Agent', exact: true }).click();
  await page.locator('[name=backend]').selectOption('"split"');
  await expect(page.locator('.agent-tools [data-tool=github_rest]')).toBeVisible();
  await expect(page.locator('.agent-tools [data-tool=github_graphql]')).toBeVisible();
  await page.screenshot({ path: info.outputPath('agent-tools.png'), fullPage: true, animations: 'disabled' });
  await selectTool(page, 'github_rest');
  const github = page.locator('#settings-tools [data-tool=github_rest]');
  await github.click();
  await expect(github).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#settings-tools [aria-pressed=true]')).toHaveCount(1);
  await expect(page.locator('[name=github_token]')).toHaveCount(1);
  const shared = page.locator('[data-config=github_token]');
  await expect(shared.locator('.config-consumers')).toHaveText('2 个工具');
  await page.locator('[name=github_token]').fill('shared-github-token');
  await shared.getByRole('button', { name: '2 个工具', exact: true }).click();
  await expect(shared.getByRole('group')).toContainText('github_rest');
  await expect(shared.getByRole('group')).toContainText('github_graphql');
  await page.screenshot({ path: info.outputPath('shared-tool-config-dark.png'), fullPage: true, animations: 'disabled' });
  await shared.getByRole('button', { name: 'github_graphql', exact: true }).click();
  await expect(page.locator('#settings-tools [data-tool=github_graphql]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('[name=github_token]')).toHaveValue('shared-github-token');
  await expect(page.getByLabel('工具结果保留提问数', { exact: true })).toBeVisible();
  await selectTool(page, 'web_search');
  await expect(page.locator('[name=github_token]')).toHaveCount(0);
  await page.locator('[name=web_search_concurrency]').fill('0');
  await expect(page.locator('#settings-tools [data-tool=web_search]').getByRole('img', { name: '不可用', exact: true })).toBeVisible();
  await page.locator('[name=web_search_concurrency]').blur();
  await expect(page.locator('[name=web_search_concurrency]')).toHaveValue('1');
  await expect(page.locator('#settings-tools [data-tool=web_search]').getByRole('img', { name: '可用', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByLabel('选择 agent')).toHaveText('GitHub');
  await page.getByRole('button', { name: '切换为浅色主题' }).click();
  await page.getByRole('button', { name: '打开设置' }).click();
  await selectTool(page, 'github_graphql');
  await page.screenshot({ path: info.outputPath('shared-tool-config-light.png'), fullPage: true, animations: 'disabled' });
});

test('unchanged agents reuse live memory across turns and refresh', async ({ page }) => {
  await login(page); await configure(page);
  const dialog = page.getByRole('dialog', { name: '继续会话', exact: true });
  const ids: string[] = [];
  for (const [index, agent] of ['Web', 'GitHub', 'Web', 'Web'].entries()) {
    await choose(page, '选择 agent', agent);
    await page.getByLabel('输入问题').fill(`question ${index + 1}`);
    await page.getByLabel('输入问题').press('Enter');
    if (index === 1 || index === 2) {
      await expect(dialog).toBeVisible();
      await dialog.getByRole('button', { name: '重建并继续', exact: true }).click();
    }
    await expect(page.locator('.execution > summary')).toHaveCount(index + 1);
    await expect(page.locator('.execution > summary').last()).toContainText('已完成');
    await expect(dialog).not.toBeVisible();
    const sessions = await (await page.request.get('/api/sessions')).json();
    expect(sessions).toHaveLength(1);
    ids.push(sessions[0].id);
  }
  expect(new Set(ids.slice(0, 3)).size).toBe(3);
  expect(ids[3]).toBe(ids[2]);
  await page.reload();
  await expect(page.getByLabel('选择 agent')).toHaveText('Web');
  await choose(page, '选择 agent', 'GitHub');
  await choose(page, '选择 agent', 'Web');
  await choose(page, '思考强度', 'Low');
  await page.getByLabel('输入问题').fill('same live instance after refresh');
  await page.getByLabel('输入问题').press('Enter');
  await expect(page.locator('.execution > summary')).toHaveCount(5);
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await expect(dialog).not.toBeVisible();
  expect((await (await page.request.get('/api/sessions')).json())[0].id).toBe(ids[3]);
});

test('rebuild is a neutral confirmation before changes, cancellation retains the live instance', async ({ page }, info) => {
  await login(page); await configure(page); await send(page, 'original evidence');
  await expect(page.locator('.execution > summary')).toContainText('已完成');
  const original = (await (await page.request.get('/api/sessions')).json())[0].id;
  await page.getByRole('button', { name: '打开设置' }).click();
  await selectTool(page, 'web_search');
  await page.locator('[name=web_search_interval]').fill('7');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByLabel('输入问题').fill('use updated tools');
  await page.getByLabel('输入问题').press('Enter');
  const dialog = page.getByRole('dialog', { name: '继续会话', exact: true });
  await expect(dialog).toContainText('将重新构造 Agent，并根据事件流恢复已观测的上下文。当前会话 Agent 内存状态可能丢失');
  await expect(dialog.locator('.danger, [role=alert]')).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('rebuild-notice.png'), fullPage: true, animations: 'disabled' });
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByLabel('输入问题')).toHaveValue('use updated tools');
  expect((await (await page.request.get('/api/sessions')).json()).map((session: { id: string }) => session.id)).toEqual([original]);
  await expect(page.locator('.execution > summary')).toHaveCount(1);
  await page.getByLabel('输入问题').press('Enter');
  await dialog.getByLabel('本会话不再提示').check();
  await dialog.getByRole('button', { name: '重建并继续', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.locator('.execution > summary')).toHaveCount(2);
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  const exported = await captureDownload(page, '导出事件');
  const starts = exported.events.filter((event: { type: string }) => event.type === 'query/start');
  expect(starts.map((event: { data: { settings: { options: { web_search_interval: number } } } }) => event.data.settings.options.web_search_interval)).toEqual([2, 7]);
  expect((await page.request.get(`/api/sessions/${original}`)).status()).toBe(404);
  await page.reload();
  await page.getByLabel('输入问题').fill('remember this conversation');
  await expect(page.getByRole('button', { name: '发送问题', exact: true })).toBeEnabled();
  await page.getByLabel('输入问题').press('Enter');
  await expect(page.locator('.execution > summary')).toHaveCount(3);
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  await expect(dialog).not.toBeVisible();
  const history = await captureDownload(page, '导出历史');
  expect(JSON.stringify(history)).not.toContain('rebuild_acknowledged');
});

test('models without reasoning do not render empty thought cards', async ({ page }) => {
  await login(page); await configure(page);
  await send(page, 'no reasoning');
  await expect(page.locator('.execution > summary')).toContainText('已完成');
  await page.locator('.execution > summary').click();
  await expect(page.locator('.trace-count')).toContainText('2 次模型请求 · 1 次工具调用');
  await expect(page.locator('.trace-item[data-kind=model]')).toHaveCount(0);
  await expect(page.locator('.trace-item[data-kind=tool]')).toHaveCount(1);
  await expect(page.locator('.trace')).not.toContainText('模型未返回 reasoning 内容');
});

test('API keys test once on paste or blur, never while typing', async ({ page }, info) => {
  const calls: string[] = [];
  page.on('request', request => {
    if (request.url().endsWith('/api/models')) calls.push('api_key');
    if (request.url().endsWith('/api/credentials/test')) calls.push(request.postDataJSON().name);
  });
  await login(page);
  await page.getByRole('button', { name: '打开设置' }).click();
  await page.getByLabel('模型地址').fill('https://model.example/v1');
  await expect(page.getByRole('heading', { name: '语言', exact: true })).toHaveJSProperty('tagName', 'H3');
  const bounds = await page.getByRole('dialog').boundingBox();
  expect(bounds!.height).toBe(820);
  for (const key of ['api_key', 'github_token', 'gitcode_token', 'brave_api_key']) {
    if (key !== 'api_key') await selectTool(page, { github_token: 'github_rest', gitcode_token: 'gitcode_api', brave_api_key: 'web_search' }[key]!);
    const input = page.locator(`[name=${key}]`);
    await input.focus();
    await input.pressSequentially('typed-test-key', { delay: 5 });
    await page.waitForTimeout(750);
    expect(calls.filter(name => name === key)).toHaveLength(0);
    await input.blur();
    await expect.poll(() => calls.filter(name => name === key).length).toBe(1);
    await expect(page.getByRole('tabpanel')).toContainText('连接成功');
    await input.focus(); await input.blur();
    expect(calls.filter(name => name === key)).toHaveLength(1);
    await input.fill('');
    await page.evaluate(() => navigator.clipboard.writeText('pasted-test-key'));
    await input.press('Control+V');
    await expect(input).toHaveValue('pasted-test-key');
    await expect.poll(() => calls.filter(name => name === key).length).toBe(2);
    await input.blur();
    await expect(page.getByRole('tabpanel')).toContainText('连接成功');
    expect(calls.filter(name => name === key)).toHaveLength(2);
  }
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByRole('button', { name: '切换为浅色主题' }).click();
  await page.getByLabel('输入问题').fill('多行草稿\n'.repeat(30));
  await expect(page.getByLabel('输入问题')).toHaveCSS('scrollbar-width', 'thin');
  await page.screenshot({ path: info.outputPath('light-composer-scroll.png'), fullPage: true, animations: 'disabled' });
});
