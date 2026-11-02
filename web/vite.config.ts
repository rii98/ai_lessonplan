/// <reference types="vitest" />
import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API the dev server proxies to (the FastAPI app). Cookies are per host, not
// per port, so a session started on :8000 via Edboox also authenticates :5173.
const API = process.env.LF_API_ORIGIN ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    // Packaged with the Python app; FastAPI serves it (see docs/FRONTEND.md §7).
    outDir: process.env.LF_WEB_OUT ?? "../src/lessonforge/api/webui",
    emptyOutDir: true,
    target: "es2020",
    sourcemap: true,
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(["/api", "/auth", "/health"].map((p) => [p, { target: API, changeOrigin: false }])),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/polyfills.ts", "./src/test/setup.ts"],
    css: { modules: { classNameStrategy: "non-scoped" } },
    restoreMocks: true,
  },
});
