import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
const backend = `http://127.0.0.1:${process.env.BACKEND_PORT || 8080}`;
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/health": { target: backend },
    },
  },
});
