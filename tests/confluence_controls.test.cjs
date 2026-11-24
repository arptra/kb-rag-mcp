// Run with: node --test tests/confluence_controls.test.cjs
// Exercise component actions against delayed API responses, without a live server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const ts = require('../apps/dashboard/node_modules/typescript');

const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../apps/dashboard/src/ConfluenceApp.tsx'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText;
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const job = (source_id, status = 'running') => ({ id: `job-${source_id}`, source_id, index_id: 'main', type: 'confluence', operation: 'sync', status, phase: 'fetch', message: 'Loading', progress: {}, result: null });
const source = (id, active_job = null) => ({ id, name: id, index_id: 'main', base_url: 'https://example.test', root_page_url: 'https://example.test/page', deployment: 'data_center', enabled: true, credential_configured: true, analyze_images: true, schedule: { kind: 'manual' }, paused: false, status: active_job?.status ?? 'idle', page_count: 2, asset_count: 2, active_job });
const textOf = node => node == null || typeof node === 'boolean' ? '' : typeof node !== 'object' ? String(node) : Array.isArray(node) ? node.map(textOf).join('') : textOf(node.props?.children);
const walk = node => node == null || typeof node !== 'object' ? [] : Array.isArray(node) ? node.flatMap(walk) : [node, ...walk(node.props?.children)];
const button = (tree, label) => walk(tree).find(node => node.type === 'button' && textOf(node) === label);
const card = (tree, id) => walk(tree).find(node => node.props?.['aria-labelledby'] === `confluence-source-${id}`);

function fixture(request) {
  const cells = []; let cursor = 0; let effects = [];
  const same = (a, b) => a && b && a.length === b.length && a.every((item, i) => Object.is(item, b[i]));
  const react = {
    useState(initial) { const i = cursor++; if (!(i in cells)) cells[i] = typeof initial === 'function' ? initial() : initial; return [cells[i], value => { cells[i] = typeof value === 'function' ? value(cells[i]) : value; }]; },
    useRef(initial) { const i = cursor++; return cells[i] ??= { current: initial }; },
    useMemo(fn, deps) { const i = cursor++; if (!same(cells[i]?.deps, deps)) cells[i] = { deps, value: fn() }; return cells[i].value; },
    useCallback(fn, deps) { return react.useMemo(() => fn, deps); },
    useEffect(fn, deps) { const i = cursor++; if (!same(cells[i]?.deps, deps)) { cells[i]?.cleanup?.(); cells[i] = { deps }; effects.push(() => { cells[i].cleanup = fn(); }); } },
  };
  const exports = {};
  const jsx = (type, props) => ({ type, props });
  const context = {
    exports, URL, URLSearchParams, AbortController, Intl, Date, Set, Map,
    window: { setTimeout: () => 1, clearTimeout() {}, confirm: () => true },
    require(name) {
      if (name === 'react') return react;
      if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx };
      if (name === './managementApi') return { useManagementApi: () => ({ request, login() {}, revision: 0 }), integrationDate: value => value || '—' };
      if (name === './accessApi') return { accessError: error => String(error) };
      if (name === './api') return { ApiError: class extends Error {} };
      if (name.endsWith('.css')) return {};
      throw new Error(`Unexpected import ${name}`);
    },
  };
  vm.runInNewContext(code, context);
  return {
    render() { cursor = 0; const tree = exports.default({ password: '', indexes: [{ id: 'main', name: 'Main' }] }); const pending = effects; effects = []; pending.forEach(fn => fn()); return tree; },
  };
}

test('a delayed start or overview does not block another source cancellation or restart', async () => {
  const start = deferred(); const overview = deferred(); let loadingInitial = true; const calls = [];
  const active = job('a');
  const app = fixture(async (url, init = {}) => {
    calls.push({ url, method: init.method });
    if (init.method === 'POST') {
      if (url.endsWith('/b/sync')) return start.promise;
      if (url.endsWith('/job-a/cancel')) return { job: { ...active, status: 'cancelled' } };
      throw new Error(`Unexpected operation ${url}`);
    }
    if (!loadingInitial) return overview.promise;
    if (url.includes('/jobs?')) return { jobs: [active] };
    return { sources: [source('a', active), source('b')], jobs: [active], can_manage: true, gigacode: { available: true } };
  });
  app.render(); await settle(); let tree = app.render(); loadingInitial = false;
  button(card(tree, 'b'), 'Синхронизировать').props.onClick(); tree = app.render();
  assert.equal(button(card(tree, 'b'), 'Синхронизировать').props.disabled, true);
  assert.equal(button(card(tree, 'a'), 'Остановить загрузку').props.disabled, false);
  button(card(tree, 'a'), 'Остановить загрузку').props.onClick(); await settle(); tree = app.render();
  assert(calls.some(call => call.url.endsWith('/job-a/cancel')));
  assert.equal(button(card(tree, 'a'), 'Синхронизировать').props.disabled, false, 'accepted cancellation must immediately release source controls despite hung overview');
  start.resolve({ job: job('b', 'queued') }); await settle(); tree = app.render();
  assert.equal(button(card(tree, 'b'), 'Убрать из очереди').props.disabled, false, 'the new job remains cancellable while refresh is pending');
});

test('source active_job remains controllable outside bounded job history', async () => {
  const active = job('a', 'queued');
  const app = fixture(async url => url.includes('/jobs?') ? { jobs: [] } : { sources: [source('a', active)], jobs: [], can_manage: true });
  app.render(); await settle(); const tree = app.render();
  assert.equal(button(card(tree, 'a'), 'Убрать из очереди').props.disabled, false);
  assert.equal(button(card(tree, 'a'), 'Синхронизировать').props.disabled, true);
  assert(button(tree, 'Отменить'), 'active job is also shown in the journal even when it is older than history limit');
});

test('stale running history cannot lock a source with authoritative active_job=null', async () => {
  const app = fixture(async url => url.includes('/jobs?') ? { jobs: [job('a')] } : { sources: [source('a')], jobs: [], can_manage: true });
  app.render(); await settle(); const tree = app.render();
  assert.equal(button(card(tree, 'a'), 'Синхронизировать').props.disabled, false);
  assert.equal(button(tree, 'Отменить'), undefined, 'do not show a stale cancellation action');
  assert(textOf(card(tree, 'a')).includes('Только по кнопке'), 'legacy analyze_images=true is not presented as enabled automatic recognition');
});
