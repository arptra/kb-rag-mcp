#!/usr/bin/env node

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDir, '..');

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith('--')) {
      throw new Error(`Unexpected argument: ${argument}`);
    }
    const value = argv[index + 1];
    if (!value || value.startsWith('--')) {
      throw new Error(`Missing value for ${argument}`);
    }
    result[argument.slice(2)] = value;
    index += 1;
  }
  return result;
}

function requireAbsoluteDirectory(value, name) {
  if (!value) {
    throw new Error(`${name} is required`);
  }
  if (!path.isAbsolute(value)) {
    throw new Error(`${name} must be an absolute path`);
  }
  if (!fs.existsSync(value) || !fs.statSync(value).isDirectory()) {
    throw new Error(`${name} does not exist or is not a directory: ${value}`);
  }
  return fs.realpathSync(value);
}

const args = parseArgs(process.argv.slice(2));
const settingsPath = path.resolve(
  args.settings
    ?? process.env.GIGACODE_SETTINGS_PATH
    ?? path.join(os.homedir(), '.gigacode', 'settings.json')
);
const modelPath = requireAbsoluteDirectory(args.model, '--model');
const dimension = Number(args.dimension);
if (!Number.isInteger(dimension) || dimension <= 0) {
  throw new Error('--dimension must be a positive integer');
}

const nodePath = path.resolve(args.node ?? process.execPath);
const serverPath = path.join(projectRoot, 'packages', 'mcp', 'dist', 'index.js');
const milvusLiteCommand = path.resolve(
  args['milvus-lite-command']
    ?? path.join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/milvus-lite.exe' : 'bin/milvus-lite')
);
if (!fs.existsSync(serverPath)) {
  throw new Error(`MCP build is missing: ${serverPath}. Run npm run build first.`);
}
if (!fs.existsSync(milvusLiteCommand)) {
  throw new Error(`Milvus Lite executable is missing: ${milvusLiteCommand}. Run scripts/setup-gigacode.sh first.`);
}

let settings = {};
if (fs.existsSync(settingsPath)) {
  settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
  const backupPath = `${settingsPath}.backup-${new Date().toISOString().replaceAll(':', '-')}`;
  fs.copyFileSync(settingsPath, backupPath);
  process.stderr.write(`Backup: ${backupPath}\n`);
}

settings.mcpServers ??= {};
settings.mcpServers['gigacode-context'] = {
  command: nodePath,
  args: [serverPath],
  env: {
    EMBEDDING_PROVIDER: 'LocalTransformer',
    LOCAL_EMBEDDING_MODEL_PATH: modelPath,
    LOCAL_EMBEDDING_DIMENSION: String(dimension),
    LOCAL_EMBEDDING_MAX_TOKENS: args['max-tokens'] ?? '2048',
    LOCAL_EMBEDDING_DTYPE: args.dtype ?? 'q8',
    LOCAL_EMBEDDING_QUERY_PREFIX: args['query-prefix'] ?? '',
    LOCAL_EMBEDDING_DOCUMENT_PREFIX: args['document-prefix'] ?? '',
    EMBEDDING_BATCH_SIZE: args['batch-size'] ?? '32',
    MILVUS_ADDRESS: args.milvus ?? '127.0.0.1:19530',
    MILVUS_LITE_COMMAND: milvusLiteCommand,
    MILVUS_LITE_DATA_DIR: path.join(projectRoot, '.runtime', 'milvus-lite'),
    GIGACODE_CONTEXT_HOME: path.join(projectRoot, '.runtime'),
    GIGACODE_CONTEXT_BACKGROUND_SYNC: 'true'
  }
};

fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, { mode: 0o600 });
process.stdout.write(`Configured gigacode-context MCP in ${settingsPath}\n`);
