import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { viteSingleFile } from 'vite-plugin-singlefile';

// 单文件内联构建 → ../../server/static/agent_monitor_viewer.html
// (hub 经 GET / 直出;产物 html 文件名取自源 html 文件 basename,故入口命名为 agent_monitor_viewer.html)
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile()],
  resolve: {
    alias: {
      // vendor 面板的模块增强统一走字符串别名(TS 不允许相对路径 declare module)
      '@dsh/ui-slots': resolve(import.meta.dirname, './src/dashboard/vendor/dsh/ui-slots/src/index.ts'),
      '@dsh/ui-conversation': resolve(import.meta.dirname, './src/dashboard/vendor/dsh/ui-conversation/src/client/index.ts'),
      '@dsh/runtime': resolve(import.meta.dirname, './src/dashboard/vendor/dsh/runtime/src/client/index.ts'),
      '@dsh/cordis': resolve(import.meta.dirname, './src/dashboard/vendor/dsh/shims/cordis.ts'),
      '@dsh/session': resolve(import.meta.dirname, './src/dashboard/vendor/dsh/session/types.ts'),
      '@dsh/session-projection': resolve(import.meta.dirname, './src/dashboard/vendor/dsh/session-projection.ts'),
      '@dsh/ui-tool': resolve(import.meta.dirname, './src/dashboard/vendor/dsh/ui-tool/src/client/index.ts'),
    },
  },
  build: {
    outDir: resolve(import.meta.dirname, '../server/static'),
    emptyOutDir: true,
    rollupOptions: {
      input: { agent_monitor_viewer: resolve(import.meta.dirname, 'agent_monitor_viewer.html') },
    },
  },
});
