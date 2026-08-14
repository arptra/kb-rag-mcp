# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Claude Context is an MCP plugin that adds semantic code search to AI coding agents. A codebase is split into chunks, embedded, and stored in a Milvus/Zilliz vector database; queries are answered by semantic (hybrid dense + sparse) search instead of loading whole directories into the model's context.

## Monorepo Layout

npm workspace (`packages/core`, `packages/mcp`). Requires Node >=20 <24 and npm >=9.

- `packages/core` (`@zilliz/claude-context-core`) — the indexing engine. All real logic lives here; the other packages are thin frontends over it.
- `packages/mcp` (`@zilliz/claude-context-mcp`) — stdio MCP server, the primary product. ESM (`"type": "module"`).
- `packages/vscode-extension` (`semanticcodesearch`) — VSCode extension. Bundled with webpack; stubs out Node-only deps (Milvus gRPC, native AST) in `src/stubs/`.
- `packages/chrome-extension` — browser build; overrides `@zilliz/milvus2-sdk-node` to `false` (no gRPC in browser).
- `examples/basic-usage` — runnable library example.

## Commands

```bash
npm ci
npm run build              # build core, then MCP
npm run build:core
npm run build:mcp
npm run dev:core           # watch core
npm run dev:mcp            # watch MCP
npm run lint               # eslint across active workspaces
npm run typecheck          # tsc --noEmit across active workspaces
npm run clean              # rimraf dist in active workspaces
```

MCP depends on the local `core` npm workspace, so **rebuild core (`npm run build:core`) before testing MCP against core changes** — it consumes `core/dist`, not its source.

### Tests

- **core** uses Jest + ts-jest. Test files are colocated as `*.test.ts` in `src/`.
  ```bash
  npm run test:core                                                  # all (runs in band)
  npm test --workspace=@zilliz/claude-context-core -- context.abort  # by filename
  npm test --workspace=@zilliz/claude-context-core -- -t "pattern"   # by test name
  ```
- **mcp** uses the Node built-in test runner via tsx (no Jest):
  ```bash
  npm run test:mcp                                                   # runs src/**/*.test.ts
  ```

### Running the MCP server locally

```bash
npm start --workspace=@zilliz/claude-context-mcp      # tsx src/index.ts
```
Configuration is entirely via environment variables (see `.env.example` and `packages/mcp/src/config.ts`). The GigaCode MCP accepts only `EMBEDDING_PROVIDER=LocalTransformer`, a local model path/dimension and a loopback `MILVUS_ADDRESS`. No cloud provider key is accepted by this runtime.

## Architecture

### Core: the `Context` orchestrator (`packages/core/src/context.ts`)

`Context` ties together three pluggable interfaces injected through its constructor config:

- **Embedding** (`src/embedding/`) — `base-embedding.ts` interface with the in-process `LocalTransformerEmbedding` implementation.
- **VectorDatabase** (`src/vectordb/`) — `MilvusVectorDatabase` (gRPC, Node-only) and `MilvusRestfulVectorDatabase` (HTTP, browser-safe). `zilliz-utils.ts` (`ClusterManager`) can provision a free Zilliz cluster and resolve an address from a token.
- **Splitter** (`src/splitter/`) — `AstCodeSplitter` (tree-sitter, the default at 2500/300 chunk/overlap) which falls back to `LangChainCodeSplitter` for unsupported languages or parse failures.

The public surface (`indexCodebase`, `reindexByChange`, `semanticSearch`, `clearIndex`, `hasIndex`) is re-exported from `src/index.ts`. Indexing reads files honoring ignore rules, splits them, embeds in batches, and upserts vectors. Collection name is derived from a hash of the absolute codebase path (overridable).

Two error types carry control-flow meaning and should be preserved when touching the pipeline:
- `IndexAbortError` — cooperative cancellation via `AbortSignal`.
- `EmbeddingError` — always re-thrown to halt the whole pipeline, unlike per-file read/parse errors which are logged and skipped. This prevents silent partial indexing (Milvus getting zero vectors while the snapshot marks files done).

### Incremental sync (`packages/core/src/sync/`)

`FileSynchronizer` builds a Merkle DAG (`merkle.ts`) of file hashes to compute `{added, removed, modified}` between runs. Snapshots persist to `~/.context/merkle/<md5-of-path>.json`. `reindexByChange` uses this so re-indexing only touches changed files. The MCP server can also run a background sync loop (`CLAUDE_CONTEXT_BACKGROUND_SYNC`, `CLAUDE_CONTEXT_SYNC_INTERVAL_MS`).

### Ignore patterns

Layered: built-in `DEFAULT_IGNORE_PATTERNS` + config + env (`CUSTOM_IGNORE_PATTERNS`) + on-disk ignore files (`.gitignore`, `.contextignore`, `.xxxignore`, and a global `~/.context/.gitignore`). `utils/ignore-matcher.ts` implements matching including gitignore negation (`!`) semantics — covered by `context.ignore-patterns.test.ts`.

### Env resolution (`packages/core/src/utils/env-manager.ts`)

`envManager.get(name)` resolves with priority `process.env` > `~/.context/.env` file. Use it instead of reading `process.env` directly so the `.env` file fallback keeps working.

### MCP server (`packages/mcp/src/`)

- `index.ts` — entry point. **Critically, it redirects `console.log`/`console.warn` to stderr at the very top**, because stdout is reserved for the MCP JSON protocol. Never write non-protocol output to stdout in this package.
- `handlers.ts` (`ToolHandlers`) — implements the tools: `index_codebase`, `search_code`, `clear_index`, `get_indexing_status`.
- `snapshot.ts` (`SnapshotManager`) — tracks per-codebase indexing state across server restarts.
- `sync.ts` (`SyncManager`) — drives incremental re-indexing.
- `config.ts`, `embedding.ts` — build `Context` (embedding provider + Milvus) from environment variables.

## Conventions

Commits follow Conventional Commits with these scopes: `core`, `vscode`, `mcp`, `examples`, `docs` (e.g. `fix(core): support gitignore negation patterns`). All code and comments are written in English.
