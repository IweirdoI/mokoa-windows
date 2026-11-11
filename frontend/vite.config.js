import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  // MOKOA FastAPI
  // Allow VITE_API_TARGET to override the Kylin V11 default port.
  const apiTarget = env.VITE_API_TARGET || 'http://127.0.0.1:18080'

  return {
    resolve: {
      alias: {
        vue: 'vue/dist/vue.esm-bundler.js',
      },
    },

    server: {
      host: '0.0.0.0',
      port: 5173,

      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },

    preview: {
      host: '0.0.0.0',
      port: 5173,

      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
      },
    },
  }
})
