import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';

// 面板测试运行器:别名与 tsconfig/vite 一致(@dsh/* 模块增强为字符串别名);
// JSX 用 esbuild automatic(与生产 plugin-react 同语义;plugin-react 在 vitest
// 无 dev-server preamble,会直接报错)。
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  resolve: {
    alias: {
      '@dsh/ui-slots': resolve(import.meta.dirname, 'src/dashboard/vendor/dsh/ui-slots/src/index.ts'),
      '@dsh/ui-conversation': resolve(import.meta.dirname, 'src/dashboard/vendor/dsh/ui-conversation/src/client/index.ts'),
      '@dsh/runtime': resolve(import.meta.dirname, 'src/dashboard/vendor/dsh/runtime/src/client/index.ts'),
      '@dsh/cordis': resolve(import.meta.dirname, 'src/dashboard/vendor/dsh/shims/cordis.ts'),
      '@dsh/session': resolve(import.meta.dirname, 'src/dashboard/vendor/dsh/session/types.ts'),
      '@dsh/session-projection': resolve(import.meta.dirname, 'src/dashboard/vendor/dsh/session-projection.ts'),
    },
  },
});
