#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { installLoopbackOnlyNetworkGuard } from '../packages/mcp/dist/offline-network.js';
import { MilvusLiteProcess } from '../packages/mcp/dist/milvus-lite-process.js';

const require = createRequire(import.meta.url);
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDir, '..');
const runtimeHome = path.join(projectRoot, '.runtime', 'smoke-home');
const codebasePath = path.join(projectRoot, 'examples', 'smoke-codebase');
const modelPath = path.join(projectRoot, 'models', 'multilingual-e5-small');

fs.mkdirSync(runtimeHome, { recursive: true });
process.env.GIGACODE_CONTEXT_HOME = runtimeHome;
process.env.HYBRID_MODE = 'false';
process.env.EMBEDDING_BATCH_SIZE = '8';
installLoopbackOnlyNetworkGuard();

const milvusLite = new MilvusLiteProcess({
  command: path.join(projectRoot, '.venv', 'bin', 'milvus-lite'),
  dataDir: path.join(projectRoot, '.runtime', 'milvus-lite-smoke'),
  host: '127.0.0.1',
  port: 19530
});
await milvusLite.ensureStarted();

const {
  Context,
  LocalTransformerEmbedding,
  MilvusVectorDatabase
} = require('../packages/core/dist/index.js');

const context = new Context({
  embedding: new LocalTransformerEmbedding({
    modelPath,
    dimension: 384,
    dtype: 'q8',
    queryPrefix: 'query: ',
    documentPrefix: 'passage: '
  }),
  vectorDatabase: new MilvusVectorDatabase({
    address: '127.0.0.1:19530',
    ssl: false,
    localOnly: true
  }),
  customExtensions: ['.yml'],
  collectionNameOverride: 'gigacode_smoke'
});

try {
  if (await context.hasIndex(codebasePath)) {
    await context.clearIndex(codebasePath);
  }

  const stats = await context.indexCodebase(codebasePath);
  const results = await context.semanticSearch(
    codebasePath,
    'Где создаётся заказ, резервируется платёж и выполняется сохранение в базу?',
    5,
    0
  );

  if (stats.indexedFiles < 4 || results.length === 0) {
    throw new Error(`End-to-end indexing failed: ${JSON.stringify({ stats, results })}`);
  }

  process.stdout.write(`${JSON.stringify({
    stats,
    topResults: results.slice(0, 3).map(result => ({
      relativePath: result.relativePath,
      score: result.score,
      lines: `${result.startLine}-${result.endLine}`
    }))
  }, null, 2)}\n`);
} finally {
  await milvusLite.stop();
}
