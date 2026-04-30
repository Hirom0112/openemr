import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: path.resolve(
      __dirname,
      '../interface/modules/custom_modules/oe-module-clinical-copilot/public'
    ),
    rollupOptions: {
      output: {
        entryFileNames: 'copilot.js',
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith('.css') ? 'copilot.css' : '[name][extname]',
      },
    },
  },
});
