import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // Real backend when VITE_MOCK is unset. The mock layer short-circuits
    // before fetch, so this proxy is only exercised against a live backend.
    //
    // The rewrite matters: apiBaseUrl is `/api` so the app calls
    // `/api/invocations`, but AgentCore serves `/invocations` at the root. The
    // prefix exists purely so a deployed ALB can route by path.
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET ?? 'http://localhost:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
