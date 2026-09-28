import { expect, test } from '@playwright/test';

test.afterEach(async ({ page }) => {
  await page.request.post('/api/auth/logout', { data: {} });
});

test('an older tab blocking history upgrades cannot leave login spinning or erase history', async ({ page, context }) => {
  const older = await context.newPage();
  await older.goto('/api/health');
  await older.evaluate(() => new Promise<void>((resolve, reject) => {
    const request = indexedDB.open('trace-agent-chat', 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      const chats = db.createObjectStore('chats', { keyPath: 'id' });
      const events = db.createObjectStore('events', { keyPath: ['chat_id', 'seq'] });
      db.createObjectStore('preferences');
      chats.put({ id: 'retained', title: '保留的历史', created: '2026-09-28T00:00:00Z', agent: 'github',
        settings: { base_url: 'https://model.example/v1', model: 'fixture-model', reasoning_effort: 'high',
          thinking: true, max_tokens: 0, options: {} } });
      events.put({ chat_id: 'retained', seq: 1, type: 'query/start', at: '2026-09-28T00:00:00Z',
        query_id: 'retained-question', data: { prompt: '原来的问题' } });
      events.put({ chat_id: 'retained', seq: 2, type: 'query/end', at: '2026-09-28T00:00:01Z',
        query_id: 'retained-question', data: { status: 'completed', answer: '原来的回答' } });
    };
    request.onsuccess = () => {
      (window as unknown as { heldDB: IDBDatabase }).heldDB = request.result;
      resolve();
    };
    request.onerror = () => reject(request.error);
  }));
  await page.goto('/');
  await expect(page.getByRole('button', { name: '进入工作空间' })).toBeEnabled();
  await expect(page.getByRole('status')).toContainText('请关闭其他 Agent Chat 标签页后刷新');
  await page.getByLabel('访问口令').fill('test-private-passphrase');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await expect(page.getByLabel('输入问题')).toBeVisible();
  await expect(page.locator('.notice')).toContainText('已有历史会保留');

  await older.close();
  await page.reload();
  await expect(page.getByLabel('输入问题')).toBeVisible();
  await expect(page.locator('.chat-open')).toContainText('保留的历史');
  await expect(page.locator('.user-message')).toContainText('原来的问题');
  await expect(page.locator('.markdown')).toContainText('原来的回答');
  await expect(page.getByText('已有历史会保留', { exact: false })).toHaveCount(0);
});

test('an open app releases its database connection for the next version', async ({ page, context }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: '进入工作空间' })).toBeEnabled();
  const newer = await context.newPage();
  await newer.goto('/api/health');
  const version = await newer.evaluate(() => new Promise<number>((resolve, reject) => {
    const request = indexedDB.open('trace-agent-chat', 3);
    request.onblocked = () => reject(new Error('The app retained its old connection'));
    request.onsuccess = () => { const db = request.result; resolve(db.version); db.close(); };
    request.onerror = () => reject(request.error);
  }));
  expect(version).toBe(3);
});
