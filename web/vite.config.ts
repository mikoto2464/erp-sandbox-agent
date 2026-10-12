import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
  build: {
    outDir: '../src/erp_agent/api/static',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: 'app.js',
        inlineDynamicImports: true,
        assetFileNames: (asset) =>
          asset.names.some((name) => name.endsWith('.css')) ? 'style.css' : '[name][extname]',
      },
    },
  },
});
