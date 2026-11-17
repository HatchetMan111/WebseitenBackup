import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build-Output landet direkt in sitebackup/static (wird mit-committed,
// der LXC-Container braucht kein Node). Dev-Proxy auf die lokale API.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../sitebackup/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8090',
    },
  },
})
