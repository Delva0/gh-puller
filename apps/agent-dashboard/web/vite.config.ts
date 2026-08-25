import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { viteSingleFile } from 'vite-plugin-singlefile';

// 单文件内联构建 → ../../server/static/agent_monitor_viewer.html
// (hub 经 GET / 直出;产物 html 文件名取自源 html 文件 basename,故入口命名为 agent_monitor_viewer.html)
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile()],
  build: {
    outDir: resolve(import.meta.dirname, '../server/static'),
    emptyOutDir: true,
    rollupOptions: {
      input: { agent_monitor_viewer: resolve(import.meta.dirname, 'agent_monitor_viewer.html') },
    },
  },
});
