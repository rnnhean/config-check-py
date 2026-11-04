import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  // 构建产物直接落到后端托管的 web/，FastAPI 用 StaticFiles 挂载，无需额外部署步骤。
  base: '/static/',
  build: {
    outDir: '../web',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    host: true,
    port: 5603,
    // 开发时前端独立起在 5603，接口全部代理给本地后端，避免 CORS 与硬编码地址。
    proxy: {
      '/api': { target: 'http://127.0.0.1:5203', changeOrigin: true },
    },
  },
})
