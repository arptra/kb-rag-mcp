import { test } from 'node:test';
import assert from 'node:assert/strict';
import { assertLoopbackUrl, isLoopbackHost } from './offline-network.js';

test('loopback policy accepts only local hosts', () => {
    assert.equal(isLoopbackHost('127.0.0.1'), true);
    assert.equal(isLoopbackHost('::1'), true);
    assert.equal(isLoopbackHost('localhost'), true);
    assert.equal(isLoopbackHost('10.0.0.10'), false);
    assert.equal(isLoopbackHost('api.openai.com'), false);
});

test('loopback policy blocks external URLs', () => {
    assert.doesNotThrow(() => assertLoopbackUrl('http://127.0.0.1:19530'));
    assert.doesNotThrow(() => assertLoopbackUrl('file:///opt/gigacode-context/model.json'));
    assert.throws(() => assertLoopbackUrl('https://huggingface.co/model'), /blocked outbound URL/);
});
