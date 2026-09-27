/** Exercise a deployed application with real models; credentials remain in process memory. */
import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium, expect } from '@playwright/test';

const url = process.env.CHAT_TEST_URL;
const password = process.env.CHAT_ACCESS_PASSWORD;
const key = process.env.OPENAI_API_KEY;
assert(url && password && key, 'Set CHAT_TEST_URL, CHAT_ACCESS_PASSWORD and OPENAI_API_KEY');
const secrets = [password, key, process.env.GH_TOKEN, process.env.GITCODE_TOKEN, process.env.BRAVE_SEARCH_API_KEY].filter(Boolean);
const output = resolve(process.env.CHAT_TEST_OUTPUT || '../verification/live-browser');
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({ baseURL: url, viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const report = { url, https: new URL(url).protocol === 'https:', started: new Date().toISOString(), queries: [] };

function clean(value) {
  const json = JSON.stringify(value, null, 2);
  assert(secrets.every(secret => !json.includes(secret)), 'Sensitive value found in an observation');
  return json;
}
async function exported() {
  const pending = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出事件', exact: true }).click();
  return JSON.parse(await readFile(await (await pending).path(), 'utf8'));
}
async function submit(prompt) {
  await page.getByLabel('输入问题').fill(prompt);
  await page.getByLabel('输入问题').press('Enter');
}

try {
  await page.goto('/', { waitUntil: 'domcontentloaded', timeout: 120_000 });
  await page.getByLabel('访问口令').fill(password);
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByLabel('输入问题')).toBeVisible({ timeout: 90_000 });
  const cookie = (await context.cookies()).find(item => item.name === 'agent_chat_access');
  assert(cookie?.httpOnly && cookie.sameSite === 'Strict');
  if (report.https) assert(cookie.secure);
  report.revision = (await (await context.request.get('/api/health')).json()).revision;
  console.log('Authenticated; testing revision ' + report.revision);
  const queries = [
    ['github', '查询 GitHub 仓库 psf/requests 的默认分支和仓库描述。只查询该仓库，简短回答并附来源链接。'],
    ['gitcode', '查询 GitCode 仓库 openharmony/docs 的默认分支和仓库描述。只查询该仓库，简短回答并附来源链接。'],
    ['web', '用网络搜索找到 Python 官方 asyncio 文档，打开官方页面核对后，用一句中文说明 asyncio 的用途，并附来源链接。'],
  ];
  for (const [agent, prompt] of queries) {
    if (agent !== 'github') await page.locator('.new-chat').click();
    await page.getByLabel('选择 agent').selectOption(agent);
    await page.getByRole('button', { name: '打开设置' }).click();
    await page.getByLabel('模型 API Key').fill(key);
    if (process.env.OPENAI_BASE_URL) await page.getByLabel('模型地址').fill(process.env.OPENAI_BASE_URL);
    if (process.env.CHAT_TEST_MODEL) await page.getByLabel('模型名', { exact: true }).fill(process.env.CHAT_TEST_MODEL);
    await page.getByLabel('最大步数').fill('8');
    await page.getByLabel('最大输出 tokens').fill('4096');
    if (agent === 'github' && process.env.GH_TOKEN) await page.getByLabel('GitHub Token').fill(process.env.GH_TOKEN);
    if (agent === 'gitcode' && process.env.GITCODE_TOKEN) await page.getByLabel('GitCode Token').fill(process.env.GITCODE_TOKEN);
    if (process.env.BRAVE_SEARCH_API_KEY) {
      await page.getByLabel('搜索服务').selectOption('brave');
      await page.getByLabel('Brave API Key').fill(process.env.BRAVE_SEARCH_API_KEY);
    } else await page.getByLabel('搜索服务').selectOption('duckduckgo');
    await page.getByRole('button', { name: '保存设置' }).click();
    console.log('Running real ' + agent + ' query');
    await submit(prompt);
    await expect(page.locator('.execution > summary').last()).toContainText(/已完成|执行失败|已停止/, { timeout: 240_000 });
    const record = await exported();
    await writeFile(resolve(output, agent + '-events.json'), clean(record));
    const end = record.events.at(-1);
    const toolStarts = record.events.filter(event => event.type === 'tool/start');
    const toolEnds = record.events.filter(event => event.type === 'tool/end');
    assert.equal(end.type, 'query/end');
    const summary = { agent, prompt, status: end.data.status, error: end.data.error, duration_ms: end.data.duration_ms,
      models: record.events.filter(event => event.type === 'model/request').length,
      tools: toolStarts.map(event => event.data.name),
      failures: toolEnds.filter(event => 'error' in event.data).length, answer: end.data.answer };
    report.queries.push(summary);
    console.log(clean(summary));
    assert.equal(end.data.status, 'completed');
    assert(toolEnds.some(event => 'result' in event.data), 'No successful tool observation');
    await page.locator('.execution > summary').last().click();
    await expect(page.locator('.trace-item').filter({ hasText: agent === 'web' ? 'web_search' : agent }).first()).toBeVisible();
    await page.screenshot({ path: resolve(output, agent + '.png'), fullPage: true });
  }

  await page.reload();
  await expect(page.locator('.markdown').last()).toBeVisible();
  await page.getByRole('button', { name: '打开设置' }).click();
  await expect(page.getByLabel('模型 API Key')).toHaveValue('');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await submit('请用更短的一句话重述刚才的回答，不需要再次调用工具。');
  await expect(page.locator('.execution > summary').last()).toContainText(/已完成|执行失败|已停止/, { timeout: 120_000 });
  await expect(page.locator('.execution > summary').last()).toContainText('已完成');
  report.refresh_and_continue = true;

  await submit('请详细检索 asyncio 的实现与文档，逐一阅读十个来源，比较不同异步调度机制。');
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible();
  await page.locator('.execution > summary').last().click();
  await expect(page.locator('.execution').last().locator('.trace-item').first()).toBeVisible();
  await page.getByRole('button', { name: '停止生成' }).click();
  await expect(page.locator('.execution > summary').last()).toContainText('已停止');
  report.stop = true;
  const final = await exported();
  assert.equal(new Set(final.events.map(event => event.seq)).size, final.events.length);
  await writeFile(resolve(output, 'refresh-stop-events.json'), clean(final));
  report.export = true;
  report.completed = true;
  console.log('Browser acceptance finished; evidence saved without credentials.');
} finally {
  report.finished = new Date().toISOString();
  await writeFile(resolve(output, 'report.json'), clean(report));
  await context.request.post('/api/auth/logout', { data: {} }).catch(() => {});
  await browser.close();
}
