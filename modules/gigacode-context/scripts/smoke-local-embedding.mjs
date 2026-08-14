#!/usr/bin/env node

import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(scriptDir, '..');
const { LocalTransformerEmbedding } = require('../packages/core/dist/index.js');

const embedding = new LocalTransformerEmbedding({
  modelPath: path.join(projectRoot, 'models', 'multilingual-e5-small'),
  dimension: 384,
  dtype: 'q8',
  queryPrefix: 'query: ',
  documentPrefix: 'passage: '
});

const query = await embedding.embed('Где реализовано создание заказа?');
const documents = await embedding.embedBatch([
  'OrderService создаёт заказ и сохраняет его в базу данных',
  'NotificationService отправляет пользователю информационное сообщение'
]);
const dot = (left, right) => left.reduce((sum, value, index) => sum + value * right[index], 0);
const queryNorm = Math.sqrt(dot(query.vector, query.vector));

if (query.dimension !== 384 || Math.abs(queryNorm - 1) > 0.001) {
  throw new Error(`Invalid local embedding output: dimension=${query.dimension}, norm=${queryNorm}`);
}

process.stdout.write(`${JSON.stringify({
  provider: embedding.getProvider(),
  dimension: query.dimension,
  queryNorm,
  orderScore: dot(query.vector, documents[0].vector),
  notificationScore: dot(query.vector, documents[1].vector)
}, null, 2)}\n`);
