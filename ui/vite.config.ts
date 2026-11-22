/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Local FastAPI backend. The browser only ever talks to the Vite origin;
// `/api/*` is proxied here with the `/api` prefix stripped.
const BACKEND = process.env.HYDRA_UI_BACKEND ?? "http://127.0.0.1:8000";

const apiProxy = {
  "/api": {
    target: BACKEND,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, "") || "/",
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
    proxy: apiProxy,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
    // Fonts are bundled from npm; keep them as files rather than inlined data URIs.
    assetsInlineLimit: 0,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
    unstubGlobals: true,
  },
});
