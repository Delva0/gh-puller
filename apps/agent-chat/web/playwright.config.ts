import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 12_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:8017',
    viewport: { width: 1440, height: 1000 },
    permissions: ['clipboard-read', 'clipboard-write'],
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'uv run --frozen uvicorn tests.fixture_server:app --host 127.0.0.1 --port 8017 --no-access-log',
    cwd: '..',
    url: 'http://127.0.0.1:8017/api/health',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
