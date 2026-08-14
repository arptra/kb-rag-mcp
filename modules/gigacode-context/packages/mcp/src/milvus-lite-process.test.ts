import assert from 'node:assert/strict';
import test from 'node:test';
import { parseMilvusLiteEndpoint } from './milvus-lite-process.js';

test('parses the default loopback Milvus Lite endpoint', () => {
    assert.deepEqual(parseMilvusLiteEndpoint('127.0.0.1:19530'), {
        host: '127.0.0.1',
        port: 19530,
    });
});

test('parses an IPv6 loopback endpoint', () => {
    assert.deepEqual(parseMilvusLiteEndpoint('[::1]:19531'), {
        host: '::1',
        port: 19531,
    });
});

test('rejects invalid ports', () => {
    assert.throws(
        () => parseMilvusLiteEndpoint('127.0.0.1:70000'),
        /Invalid URL|Invalid Milvus Lite port/,
    );
});
