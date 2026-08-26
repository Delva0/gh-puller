import { resolve } from 'node:path';
import { defineConfig } from 'vitest/config';

// vitest 运行时别名(与 apps/agent-dashboard/web/{tsconfig,vite.config} 保持一致):
// vendor 面板的模块增强走字符串别名(TS 不允许相对路径 declare module)。
// JSX 用 esbuild automatic(与生产 plugin-react 同语义;viruntest 内 plugin-react 无
// dev-server preamble,会直接报错)。
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  resolve: {
    alias: {
      '@dsh/ui-slots': resolve(__dirname, 'src/vendor/dsh/ui-slots/src/index.ts'),
      '@dsh/ui-conversation': resolve(__dirname, 'src/vendor/dsh/ui-conversation/src/client/index.ts'),
      '@dsh/runtime': resolve(__dirname, 'src/vendor/dsh/runtime/src/client/index.ts'),
      '@dsh/cordis': resolve(__dirname, 'src/vendor/dsh/shims/cordis.ts'),
      '@dsh/session': resolve(__dirname, 'src/vendor/dsh/session/types.ts'),
      '@dsh/session-projection': resolve(__dirname, 'src/vendor/dsh/session-projection.ts'),
    },
  },
});
