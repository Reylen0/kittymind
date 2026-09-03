import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// root = electron/src/renderer/
// outDir relative to root → electron/renderer-dist/
export default defineConfig({
  plugins: [react()],
  root: 'src/renderer',
  base: './',
  build: {
    outDir: '../../renderer-dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
})
