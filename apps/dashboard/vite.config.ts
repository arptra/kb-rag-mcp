import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = fileURLToPath(new URL("../..", import.meta.url));
const tlsEnabled = !["0", "false", "no", "off"].includes(
  (process.env.VITE_TLS_ENABLED ?? "true").toLowerCase(),
);
const certificate = process.env.VITE_TLS_CERT_FILE ?? resolve(projectRoot, "certs/server.crt");
const privateKey = process.env.VITE_TLS_KEY_FILE ?? resolve(projectRoot, "certs/server.key");
const tls = tlsEnabled && existsSync(certificate) && existsSync(privateKey)
  ? { cert: readFileSync(certificate), key: readFileSync(privateKey) }
  : undefined;

export default defineConfig(({ command }) => {
  if (command === "serve" && tlsEnabled && tls === undefined) {
    throw new Error(
      `HTTPS certificate pair is missing: ${certificate}, ${privateKey}. `
      + "Run ../../scripts/generate-dev-certs.sh first.",
    );
  }
  return {
    base: "/admin/",
    plugins: [react()],
    build: {
      outDir: "../../src/corporate_kb/mcp/admin_dist",
      emptyOutDir: true,
    },
    server: {
      https: tls,
      proxy: {
        "/admin/api": {
          target: process.env.VITE_BACKEND_URL ?? "https://127.0.0.1:8000",
          secure: false,
        },
      },
    },
  };
});
