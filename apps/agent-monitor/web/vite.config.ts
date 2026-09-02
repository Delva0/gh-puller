import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { viteSingleFile } from 'vite-plugin-singlefile';

// The hub serves this single-file build directly.
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
