#!/usr/bin/env node

import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { Client } from '../packages/mcp/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import { StdioClientTransport } from '../packages/mcp/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDir, '..');
const codebasePath = path.join(projectRoot, 'examples', 'smoke-codebase');
const serverPath = path.join(projectRoot, 'packages', 'mcp', 'dist', 'index.js');

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [serverPath],
  cwd: projectRoot,
  stderr: 'pipe',
  env: {
    EMBEDDING_PROVIDER: 'LocalTransformer',
    LOCAL_EMBEDDING_MODEL_PATH: path.join(projectRoot, 'models', 'multilingual-e5-small'),
    LOCAL_EMBEDDING_DIMENSION: '384',
    LOCAL_EMBEDDING_DTYPE: 'q8',
    LOCAL_EMBEDDING_QUERY_PREFIX: 'query: ',
    LOCAL_EMBEDDING_DOCUMENT_PREFIX: 'passage: ',
    EMBEDDING_BATCH_SIZE: '8',
    MILVUS_ADDRESS: '127.0.0.1:19530',
    MILVUS_LITE_COMMAND: path.join(projectRoot, '.venv', 'bin', 'milvus-lite'),
    MILVUS_LITE_DATA_DIR: path.join(projectRoot, '.runtime', 'milvus-lite-smoke'),
    CODE_CHUNKS_COLLECTION_NAME_OVERRIDE: 'gigacode_smoke',
    GIGACODE_CONTEXT_HOME: path.join(projectRoot, '.runtime', 'smoke-home'),
    GIGACODE_CONTEXT_BACKGROUND_SYNC: 'false',
    GIGACODE_CONTEXT_TRIGGER_WATCHER: 'false',
    HYBRID_MODE: 'false'
  }
});

const client = new Client({ name: 'gigacode-context-smoke', version: '1.0.0' });

try {
  await client.connect(transport);
  const toolList = await client.listTools();
  const toolNames = toolList.tools.map(tool => tool.name).sort();
  const expectedTools = ['clear_index', 'get_indexing_status', 'index_codebase', 'search_code'];
  if (JSON.stringify(toolNames) !== JSON.stringify(expectedTools)) {
    throw new Error(`Unexpected MCP tools: ${toolNames.join(', ')}`);
  }

  if (process.env.GIGACODE_SMOKE_SKIP_INDEX !== 'true') {
    const indexing = await client.callTool({
      name: 'index_codebase',
      arguments: {
        path: codebasePath,
        force: true,
        splitter: 'ast',
        customExtensions: ['.yml']
      }
    });
    if (indexing.isError) {
      throw new Error(`MCP indexing failed to start: ${JSON.stringify(indexing)}`);
    }

    const deadline = Date.now() + 60_000;
    let indexed = false;
    while (Date.now() < deadline) {
      const status = await client.callTool({
        name: 'get_indexing_status',
        arguments: { path: codebasePath }
      });
      const statusText = Array.isArray(status.content)
        ? status.content.map(item => item.type === 'text' ? item.text : '').join('\n')
        : '';
      if (/fully indexed and ready for search/i.test(statusText)) {
        indexed = true;
        break;
      }
      if (status.isError || /failed/i.test(statusText)) {
        throw new Error(`MCP indexing failed: ${JSON.stringify(status)}`);
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    if (!indexed) throw new Error('MCP indexing timed out after 60 seconds');
  }

  const result = await client.callTool({
    name: 'search_code',
    arguments: {
      path: codebasePath,
      query: 'Где создаётся заказ и вызывается платёжный сервис?',
      limit: 3
    }
  });

  if (result.isError || !Array.isArray(result.content) || result.content.length === 0) {
    throw new Error(`MCP search failed: ${JSON.stringify(result)}`);
  }

  process.stdout.write(`${JSON.stringify({
    protocol: 'MCP stdio',
    tools: toolNames,
    searchResult: result.content
  }, null, 2)}\n`);
} finally {
  await client.close();
}
