import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

// root = electron/src/renderer/
// outDir relative to root → electron/renderer-dist/
export default defineConfig({
  plugins: [react()],
  root: 'src/renderer',
  base: './',
  build: {
    outDir: '../../renderer-dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        index: resolve(__dirname, 'src/renderer/index.html'),
        // 带 mock 数据的预览页，仅用于浏览器内验证 UI，不参与应用启动
        preview: resolve(__dirname, 'src/renderer/preview.html'),
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
  },
})
