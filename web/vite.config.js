import { defineConfig } from "vite";

// Wiki frontend (ROADMAP D5/D18). Production build is mounted by main.py under
// `/wiki`, so `base` is set to `/wiki/`. Dev server proxies the API to the
// backend so the two can be developed independently.
export default defineConfig({
  base: "/wiki/",
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/media": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
