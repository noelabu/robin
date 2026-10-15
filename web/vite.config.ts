import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build is bundled into the Python package and served by the Robin service from the same origin.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/robin/web/dist", emptyOutDir: true, sourcemap: false },
  server: { proxy: { "/api": "http://127.0.0.1:7333" } },
  test: { environment: "jsdom", globals: true, css: { include: [/styles\.css/] } },
});
