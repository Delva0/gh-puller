import { defineConfig } from 'vitest/config';

// The test runner uses automatic JSX without the Vite development preamble.
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  test: { setupFiles: ['./src/test-setup.ts'] },
});
