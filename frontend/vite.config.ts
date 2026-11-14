import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Server-only configuration: no credentials or backend URL injected into JS.
  const env = loadEnv(mode, process.cwd(), 'ORCHESTRATOR_DEV_API_TARGET')
  const apiTarget = process.env.ORCHESTRATOR_DEV_API_TARGET
    || env.ORCHESTRATOR_DEV_API_TARGET || 'http://localhost:8080'
  const target = new URL(apiTarget)
  if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password
      || target.pathname !== '/' || target.search || target.hash) {
    throw new Error('ORCHESTRATOR_DEV_API_TARGET must be an HTTP(S) origin without credentials')
  }
  return {
    plugins: [react()],
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (!id.includes('node_modules')) {
              return undefined
            }
            if (id.includes('react-router')) {
              return 'router'
            }
            if (id.includes('react')) {
              return 'react-vendor'
            }
            if (id.includes('axios')) {
              return 'network'
            }
            if (id.includes('lucide-react')) {
              return 'icons'
            }
            return 'vendor'
          },
        },
      },
    },
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      port: 3000,
      strictPort: true,
      host: '0.0.0.0',
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
          ws: true, // Enable WebSocket proxying
        },
      },
    },
  }
})
