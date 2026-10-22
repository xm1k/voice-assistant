import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {'/api': {target: 'http://localhost:8001', ws: true, changeOrigin: false}},
  },
  build: {outDir: 'dist', emptyOutDir: true},
});
