import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The BFF serves the built app and proxies /api. In dev, proxy /api to the BFF.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist' },
  server: {
    proxy: { '/api': 'http://localhost:5174' },
  },
})
