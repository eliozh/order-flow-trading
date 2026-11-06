import react from '@vitejs/plugin-react'
import { configDefaults, defineConfig } from 'vitest/config'

const apiTarget = process.env.VITE_DEV_API_TARGET ?? 'http://127.0.0.1:7611'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Rewrite the Host header to the daemon origin (127.0.0.1:7611); the API
    // rejects foreign Host headers (HOST_FORBIDDEN / DNS rebinding).
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
      '/health': { target: apiTarget, changeOrigin: true },
      '/ready': { target: apiTarget, changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    css: false,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
})
