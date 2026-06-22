import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
// Dev server proxies /api -> the FastAPI backend (read-dashboard-spec §6); the
// build emits a single static bundle the backend serves under / at deploy time.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    rolldownOptions: {
      output: {
        // Split the heavy viz libs (recharts, xyflow) into their own chunks so
        // the app shell stays small and the libs cache across deploys.
        codeSplitting: {
          groups: [
            { name: 'recharts', test: /node_modules\/(recharts|d3-|victory-|decimal\.js)/ },
            { name: 'xyflow', test: /node_modules\/(@xyflow|@reactflow|d3-zoom|d3-drag|d3-selection|zustand|classcat)/ },
          ],
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
