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
        return html
          .replaceAll(
            "import('/@domscribe/react-init.js')",
            "import('/admin/@domscribe/react-init.js')",
          )
          // React init imports the pre-bundled overlay. Drop the base plugin's
          // second /node_modules import, which ignores Vite's /admin/ base and
          // can register the same custom elements twice after reconnects.
          .replaceAll(
            "import('/node_modules/@domscribe/overlay/index.js').then(m => m.initOverlay()).catch(e => console.warn('[domscribe] Failed to load overlay:', e.message));",
            "",
          );
      },
    },
  };
}

const tls = tlsEnabled && existsSync(certificate) && existsSync(privateKey)
  ? { cert: readFileSync(certificate), key: readFileSync(privateKey) }
  : undefined;

export default defineConfig(({ command, mode }) => {
  const domscribeEnabled = command === "serve" && mode === "domscribe";
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
