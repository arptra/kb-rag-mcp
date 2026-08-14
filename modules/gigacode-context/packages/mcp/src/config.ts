import { readFileSync } from "node:fs";
import { envManager, isLoopbackMilvusAddress, LocalEmbeddingDtype } from "@zilliz/claude-context-core";

export interface ContextMcpConfig {
    name: string;
    version: string;
    embeddingProvider: 'LocalTransformer';
    embeddingModelPath: string;
    embeddingDimension: number;
    embeddingMaxTokens: number;
    embeddingQueryPrefix: string;
    embeddingDocumentPrefix: string;
    embeddingDtype: LocalEmbeddingDtype;
    milvusAddress: string;
    milvusLiteCommand: string;
    milvusLiteDataDir: string;
    collectionNameOverride?: string;
}

// Legacy format (v1) - for backward compatibility
export interface CodebaseSnapshotV1 {
    indexedCodebases: string[];
    indexingCodebases: string[] | Record<string, number>;  // Array (legacy) or Map of codebase path to progress percentage
    lastUpdated: string;
}

// New format (v2) - structured with codebase information

export type RequestSplitterType = 'ast' | 'langchain';

// Request-level indexing options stored with a codebase's snapshot entry.
export interface CodebaseIndexOptions {
    requestSplitter?: RequestSplitterType;
    requestCustomExtensions?: string[];
    requestIgnorePatterns?: string[];
}

// Base interface for common fields
interface CodebaseInfoBase extends CodebaseIndexOptions {
    lastUpdated: string;
}

// Indexing state - when indexing is in progress
export interface CodebaseInfoIndexing extends CodebaseInfoBase {
    status: 'indexing';
    indexingPercentage: number;  // Current progress percentage
}

// Indexed state - when indexing completed successfully
export interface CodebaseInfoIndexed extends CodebaseInfoBase {
    status: 'indexed';
    indexedFiles: number;        // Number of files indexed
    totalChunks: number;         // Total number of chunks generated
    indexStatus: 'completed' | 'limit_reached';  // Status from indexing result
}

// Index failed state - when indexing failed
export interface CodebaseInfoIndexFailed extends CodebaseInfoBase {
    status: 'indexfailed';
    errorMessage: string;        // Error message from the failure
    lastAttemptedPercentage?: number;  // Progress when failure occurred
}

// Union type for all codebase information states
export type CodebaseInfo = CodebaseInfoIndexing | CodebaseInfoIndexed | CodebaseInfoIndexFailed;

export interface CodebaseSnapshotV2 {
    formatVersion: 'v2';
    codebases: Record<string, CodebaseInfo>;  // codebasePath -> CodebaseInfo
    lastUpdated: string;
}

// Union type for all supported formats
export type CodebaseSnapshot = CodebaseSnapshotV1 | CodebaseSnapshotV2;

function readMcpPackageVersion(): string {
    try {
        const packageJsonUrl = new URL("../package.json", import.meta.url);
        const packageJson = JSON.parse(readFileSync(packageJsonUrl, "utf8")) as { version?: unknown };
        if (typeof packageJson.version === "string" && packageJson.version.trim()) {
            return packageJson.version;
        }
    } catch (error) {
        console.warn(`[DEBUG] ⚠️  Unable to read MCP package version: ${error}`);
    }

    return "1.0.0";
}

const defaultMcpServerVersion = readMcpPackageVersion();

function getPositiveIntegerFromEnv(name: string): number | undefined {
    const rawValue = envManager.get(name);
    if (!rawValue) {
        return undefined;
    }

    const parsedValue = Number(rawValue);
    if (Number.isInteger(parsedValue) && parsedValue > 0) {
        return parsedValue;
    }

    console.warn(`[DEBUG] ⚠️  Ignoring invalid ${name}: ${rawValue}. Expected a positive integer.`);
    return undefined;
}

function requireEnv(name: string): string {
    const value = envManager.get(name)?.trim();
    if (!value) {
        throw new Error(`${name} is required in offline mode`);
    }
    return value;
}

function getPrefixFromEnv(name: string): string {
    const value = envManager.get(name) || '';
    if (value.length >= 2) {
        const first = value[0];
        const last = value[value.length - 1];
        if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
            return value.slice(1, -1);
        }
    }
    return value;
}

function getLocalEmbeddingDtype(): LocalEmbeddingDtype {
    const value = envManager.get('LOCAL_EMBEDDING_DTYPE') || 'q8';
    const supported: LocalEmbeddingDtype[] = ['fp32', 'fp16', 'int8', 'uint8', 'q8', 'q4', 'q4f16', 'bnb4'];
    if (!supported.includes(value as LocalEmbeddingDtype)) {
        throw new Error(`Unsupported LOCAL_EMBEDDING_DTYPE=${value}; expected one of ${supported.join(', ')}`);
    }
    return value as LocalEmbeddingDtype;
}

export function createMcpConfig(): ContextMcpConfig {
    const provider = envManager.get('EMBEDDING_PROVIDER') || 'LocalTransformer';
    if (provider !== 'LocalTransformer') {
        throw new Error(`Offline build only supports EMBEDDING_PROVIDER=LocalTransformer; received ${provider}`);
    }

    const embeddingDimension = getPositiveIntegerFromEnv('LOCAL_EMBEDDING_DIMENSION');
    if (!embeddingDimension) {
        throw new Error('LOCAL_EMBEDDING_DIMENSION is required and must be a positive integer');
    }

    const milvusAddress = requireEnv('MILVUS_ADDRESS');
    if (!isLoopbackMilvusAddress(milvusAddress)) {
        throw new Error(`Offline mode requires a loopback MILVUS_ADDRESS; received ${milvusAddress}`);
    }

    const config: ContextMcpConfig = {
        name: envManager.get('MCP_SERVER_NAME') || "GigaCode Context MCP",
        version: envManager.get('MCP_SERVER_VERSION') || defaultMcpServerVersion,
        embeddingProvider: 'LocalTransformer',
        embeddingModelPath: requireEnv('LOCAL_EMBEDDING_MODEL_PATH'),
        embeddingDimension,
        embeddingMaxTokens: getPositiveIntegerFromEnv('LOCAL_EMBEDDING_MAX_TOKENS') || 2048,
        embeddingQueryPrefix: getPrefixFromEnv('LOCAL_EMBEDDING_QUERY_PREFIX'),
        embeddingDocumentPrefix: getPrefixFromEnv('LOCAL_EMBEDDING_DOCUMENT_PREFIX'),
        embeddingDtype: getLocalEmbeddingDtype(),
        milvusAddress,
        milvusLiteCommand: requireEnv('MILVUS_LITE_COMMAND'),
        milvusLiteDataDir: requireEnv('MILVUS_LITE_DATA_DIR'),
        collectionNameOverride: envManager.get('CODE_CHUNKS_COLLECTION_NAME_OVERRIDE')
    };

    return config;
}

export function logConfigurationSummary(config: ContextMcpConfig): void {
    console.log(`[MCP] 🚀 Starting GigaCode Context MCP in offline mode`);
    console.log(`[MCP] Configuration Summary:`);
    console.log(`[MCP]   Server: ${config.name} v${config.version}`);
    console.log(`[MCP]   Embedding Provider: ${config.embeddingProvider}`);
    console.log(`[MCP]   Local Model: ${config.embeddingModelPath}`);
    console.log(`[MCP]   Embedding Dimension: ${config.embeddingDimension}`);
    console.log(`[MCP]   Embedding DType: ${config.embeddingDtype}`);
    console.log(`[MCP]   Milvus Address: ${config.milvusAddress}`);
    console.log(`[MCP]   Milvus Lite Command: ${config.milvusLiteCommand}`);
    console.log(`[MCP]   Milvus Lite Data: ${config.milvusLiteDataDir}`);
    console.log(`[MCP]   External providers: disabled`);
    if (config.collectionNameOverride) {
        console.log(`[MCP]   Collection Name Override: ✅ Configured`);
    }
    console.log(`[MCP] 🔧 Initializing server components...`);
}

export function showHelpMessage(): void {
    console.log(`
GigaCode Context MCP (offline)

Usage: node /absolute/path/to/packages/mcp/dist/index.js [options]

Options:
  --help, -h                          Show this help message

Environment Variables:
  MCP_SERVER_NAME                  Server name (default: GigaCode Context MCP)
  MCP_SERVER_VERSION               Server version
  EMBEDDING_PROVIDER               Must be LocalTransformer
  LOCAL_EMBEDDING_MODEL_PATH       Absolute path to bundled Transformers.js model
  LOCAL_EMBEDDING_DIMENSION        Exact model output dimension
  LOCAL_EMBEDDING_MAX_TOKENS       Maximum input tokens (default: 2048)
  LOCAL_EMBEDDING_DTYPE            ONNX model dtype (default: q8)
  LOCAL_EMBEDDING_QUERY_PREFIX     Optional prefix for search queries
  LOCAL_EMBEDDING_DOCUMENT_PREFIX  Optional prefix for indexed chunks
  MILVUS_ADDRESS                   Required loopback address, for example 127.0.0.1:19530
  MILVUS_LITE_COMMAND              Absolute path to the local milvus-lite executable
  MILVUS_LITE_DATA_DIR             Absolute persistent database directory
  CODE_CHUNKS_COLLECTION_NAME_OVERRIDE
                          Optional readable prefix for collection names.
                          Uses code_chunks_<override>_<pathHash> (or hybrid_...)
                          after sanitization (letters/digits/underscore, 255 chars max).
                          The per-codebase pathHash is preserved so multiple
                          codebases stay distinct under the same override.

  MCP Sync Configuration:
  GIGACODE_CONTEXT_BACKGROUND_SYNC
                          Enable/disable startup + periodic background sync
                          for indexed codebases (default: true). Set to false
                          to disable polling while keeping trigger-based sync.
  GIGACODE_CONTEXT_SYNC_INTERVAL_MS
                          Background sync interval in milliseconds when enabled
                          (default: 300000).

  Sync Trigger Watcher:
  GIGACODE_CONTEXT_TRIGGER_WATCHER
                          Enable/disable the ~/.context/.sync-trigger filesystem
                          watcher (default: true). When enabled, touching the
                          trigger file kicks off an immediate, debounced re-index.
                          Triggered syncs share the same global cross-process
                          lock as background sync, so multi-instance setups stay
                          coordinated. Set to false to disable filesystem
                          watching entirely (read-only / sandboxed environments).

Example:
  EMBEDDING_PROVIDER=LocalTransformer \\
  LOCAL_EMBEDDING_MODEL_PATH=/opt/gigacode-context/models/multilingual-e5-base \\
  LOCAL_EMBEDDING_DIMENSION=768 \\
  MILVUS_ADDRESS=127.0.0.1:19530 \\
  node /opt/gigacode-context/packages/mcp/dist/index.js
        `);
}
