import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { domscribe } from "@domscribe/react/vite";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const tlsEnabled = !["0", "false", "no", "off"].includes(
  (process.env.VITE_TLS_ENABLED ?? "true").toLowerCase(),
);
const certificate = process.env.VITE_TLS_CERT_FILE ?? resolve(projectRoot, "certs/server.crt");
const privateKey = process.env.VITE_TLS_KEY_FILE ?? resolve(projectRoot, "certs/server.key");
type DomscribeOptions = NonNullable<Parameters<typeof domscribe>[0]> & { rootDir?: string };
const domscribeOptions: DomscribeOptions = {
  rootDir: projectRoot,
  relay: { autoStart: true, host: "127.0.0.1" },
  overlay: { initialMode: "expanded" },
  runtime: { redactPII: true },
};

function domscribeBasePath(): Plugin {
  return {
    name: "domscribe-admin-base-path",
    enforce: "post",
    transformIndexHtml: {
      order: "post",
      handler(html) {
        return html.replaceAll(
          "import('/@domscribe/react-init.js')",
          "import('/admin/@domscribe/react-init.js')",
        );
      },
    },
  };
}

const tls = tlsEnabled && existsSync(certificate) && existsSync(privateKey)
  ? { cert: readFileSync(certificate), key: readFileSync(privateKey) }
  : undefined;

export default defineConfig(({ command }) => {
  const domscribeEnabled = command === "serve"
    && ["1", "true", "yes", "on"].includes(
      (process.env.VITE_DOMSCRIBE_ENABLED ?? "false").toLowerCase(),
    );
  if (command === "serve" && tlsEnabled && tls === undefined) {
    throw new Error(
      `HTTPS certificate pair is missing: ${certificate}, ${privateKey}. `
      + "Run ../../scripts/generate-dev-certs.sh first.",
    );
  }
  return {
    base: "/admin/",
    plugins: [
      react(),
      ...(domscribeEnabled ? [domscribe(domscribeOptions), domscribeBasePath()] : []),
    ],
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
