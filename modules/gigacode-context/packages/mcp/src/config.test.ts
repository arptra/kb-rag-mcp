import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createMcpConfig } from "./config.js";

const mcpPackage = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf8")
) as { version: string };

function withEnvOverride(name: string, value: string | undefined, run: () => void): void {
    const originalValue = process.env[name];

    if (value === undefined) {
        delete process.env[name];
    } else {
        process.env[name] = value;
    }

    try {
        run();
    } finally {
        if (originalValue === undefined) {
            delete process.env[name];
        } else {
            process.env[name] = originalValue;
        }
    }
}

function withRequiredOfflineEnv(run: () => void): void {
    const values: Record<string, string> = {
        EMBEDDING_PROVIDER: "LocalTransformer",
        LOCAL_EMBEDDING_MODEL_PATH: "/opt/gigacode-context/models/test-model",
        LOCAL_EMBEDDING_DIMENSION: "768",
        MILVUS_ADDRESS: "127.0.0.1:19530",
        MILVUS_LITE_COMMAND: "/opt/gigacode-context/.venv/bin/milvus-lite",
        MILVUS_LITE_DATA_DIR: "/opt/gigacode-context/.runtime/milvus-lite"
    };
    const originals = new Map<string, string | undefined>();

    for (const [name, value] of Object.entries(values)) {
        originals.set(name, process.env[name]);
        process.env[name] = value;
    }

    try {
        run();
    } finally {
        for (const [name, value] of originals) {
            if (value === undefined) {
                delete process.env[name];
            } else {
                process.env[name] = value;
            }
        }
    }
}

test("uses the MCP package version as the default server version", () => {
    withRequiredOfflineEnv(() => {
        withEnvOverride("MCP_SERVER_VERSION", undefined, () => {
            const config = createMcpConfig();

            assert.equal(config.version, mcpPackage.version);
        });
    });
});

test("allows MCP_SERVER_VERSION to override the package default", () => {
    withRequiredOfflineEnv(() => {
        withEnvOverride("MCP_SERVER_VERSION", "custom-test-version", () => {
            const config = createMcpConfig();

            assert.equal(config.version, "custom-test-version");
        });
    });
});

test("rejects external embedding providers", () => {
    withRequiredOfflineEnv(() => {
        withEnvOverride("EMBEDDING_PROVIDER", "OpenAI", () => {
            assert.throws(() => createMcpConfig(), /only supports EMBEDDING_PROVIDER=LocalTransformer/);
        });
    });
});

test("rejects a non-loopback Milvus address", () => {
    withRequiredOfflineEnv(() => {
        withEnvOverride("MILVUS_ADDRESS", "milvus.example.internal:19530", () => {
            assert.throws(() => createMcpConfig(), /requires a loopback MILVUS_ADDRESS/);
        });
    });
});
