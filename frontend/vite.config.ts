import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// One .env for the whole project: it lives in the repo root, next to sdoc/.
// Only VITE_* variables are ever exposed to the browser - the LLM key is not.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '..', ['SDOC_', 'VITE_'])
  const apiPort = env.SDOC_API_PORT || '8000'

  return {
    plugins: [react()],
    envDir: '..',
    server: {
      // dev: `npm run dev` forwards /api to `python -m sdoc serve`
      proxy: {
        '/api': `http://127.0.0.1:${apiPort}`,
      },
    },
  }
})
