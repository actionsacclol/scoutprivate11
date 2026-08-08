import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import electron from 'vite-plugin-electron';
import rendererPlugin from 'vite-plugin-electron-renderer';
import path from 'node:path';

// Krypt Scout is a single main window app. The Python FastAPI backend is
// spawned as a child process by the main process (see
// electron/system/python-backend.ts); the renderer talks HTTP to it on
// 127.0.0.1:<port> resolved once via window.scout.backend.port().

export default defineConfig({
  plugins: [
    react(),
    electron([
      {
        entry: 'electron/main.ts',
        vite: {
          build: {
            outDir: 'dist-electron',
            sourcemap: false,
            rollupOptions: {
              // Native/optional deps stay external — resolved from
              // node_modules at runtime, not bundled by rollup.
              // electron-updater must stay external too: it locates
              // resources/app-update.yml and package.json relative to its
              // own module tree, which bundling would break.
              external: ['discord-rpc', 'electron-updater'],
            },
          },
        },
      },
      {
        entry: 'electron/preload.ts',
        onstart(options) {
          options.reload();
        },
        vite: { build: { outDir: 'dist-electron', sourcemap: false } },
      },
    ]),
    rendererPlugin(),
  ],
  resolve: {
    alias: {
      '@shared': path.resolve(__dirname, 'shared'),
    },
  },
  server: {
    port: 5183,
    strictPort: true,
    // Fallback for running plain `vite` without Electron: relative /api
    // requests proxy to a locally-started uvicorn on the default port.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  clearScreen: false,
});
