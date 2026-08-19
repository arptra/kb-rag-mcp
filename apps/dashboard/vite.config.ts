import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/admin/",
  plugins: [react()],
  build: {
    outDir: "../../src/corporate_kb/mcp/admin_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/admin/api": "http://127.0.0.1:8000",
    },
  },
});
