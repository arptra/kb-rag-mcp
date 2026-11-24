// node --test tests/harness_control_ui.test.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const directory = path.join(__dirname, "../src/agent_harness/control/static");

class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.value = ""; this.hidden = false; this.text = ""; }
  append(...items) { for (const item of items) if (item.tagName === "fragment") this.append(...item.children); else this.children.push(item); }
  replaceChildren(...items) { this.children = []; this.text = ""; this.append(...items); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map((item) => item.textContent).join(""); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setCustomValidity(value) { this.validationMessage = value; }
  querySelectorAll() { return this.children.flatMap((child) => [child, ...child.querySelectorAll()]).filter((item) => ["input", "select"].includes(item.tagName)); }
  focus() {} showModal() {} close() {}
}
function settings() {
  const profile = { model_calls: 8, summary_calls: 4, source_calls: 20, context_tokens: 16000 };
  return { can_manage: true, revision: 7, settings: {
    profiles: Object.fromEntries(["quick", "investigate", "autonomous"].map((mode) => [mode, Object.fromEntries(["low", "medium", "high"].map((effort) => [effort, { ...profile }]))])),
    autonomy: { enabled: true, max_extensions: 4, minimum_progress: true, caps: { ...profile, model_calls: 128 } },
    context: { enabled: true, window_tokens: 65536, keep_fraction: .35 },
    runtime: { queue_max: 32, quick_workers: 2, external_tool_timeout_seconds: 600, max_decision_errors: 3 },
  }, schema: { $defs: { RunLimits: { properties: { model_calls: { type: "integer", minimum: 1, maximum: 1000 } } }, AutonomySettings: { properties: {} }, ContextSettings: { properties: {} }, RuntimeLimits: { properties: {} } } }, activation: { restart_fields: ["quick_workers", "queue_max"], restart_required: [] } };
}
function metrics() {
  return { summary: { runs: 2, active_now: 1, model_calls: 6, summary_calls: 2, duration_mean_ms: 1250, input_tokens_reported: null, input_tokens_estimated: 3000, reported_usage_runs: 0 },
    series: [{ date: "2026-11-24", runs: 2, completed: 1, failed: 0, model_calls: 6, source_calls: 7, duration_mean_ms: 1250, duration_p95_ms: 1400, extensions: 2, compactions: 1 }],
    stop_reasons: [{ reason: "completed", label: "Ответ завершён", count: 1 }], queues: { quick: { queued: 0, workers: 2 } },
    active_runs: [{ id: "older-active", created_at: "2026-10-01T10:00:00Z", mode: "autonomous", status: "running", model_calls: 2, duration_ms: 0 }], recent_runs: [],
  };
}
function fixture(t, handler = () => metrics()) {
  const nodes = new Map([...fs.readFileSync(path.join(directory, "index.html"), "utf8").matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  nodes.get("profile-mode").value = "autonomous"; nodes.get("profile-effort").value = "medium"; nodes.get("days").value = "7";
  const timers = new Set(), calls = [], charts = [];
  const document = { baseURI: "http://localhost/harness/control/", hidden: false, getElementById(id) { assert(nodes.has(id), "Missing element " + id); return nodes.get(id); }, createElement: (tag) => new Element(tag), createDocumentFragment: () => new Element("fragment"), addEventListener() {} };
  const window = { addEventListener() {}, matchMedia: () => ({ matches: true }), setTimeout(callback, delay) { const timer = setTimeout(callback, delay); timers.add(timer); return timer; }, clearTimeout(timer) { clearTimeout(timer); timers.delete(timer); }, Chart: class { constructor(element, config) { this.config = config; charts.push(this); } destroy() { this.destroyed = true; } } };
  const context = vm.createContext({ document, window, URL, AbortController, console, sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} }, async fetch(url, options) { const request = { route: url.pathname.replace("/harness/api/control/", "") + url.search, ...options }; calls.push(request); const response = url.pathname === "/access/api/session" ? { csrf_token: "test-admin-csrf" } : await handler(request); return { ok: !response.status || response.status < 400, status: response.status || 200, json: async () => response.payload || response }; } });
  const script = fs.readFileSync(path.join(directory, "app.js"), "utf8").replace(/  loadSettings\(\);\s*\}\)\(\);\s*$/, "globalThis.ui = { state, api, applySettings, renderMetrics, refreshMetrics, saveSettings, cancelRun, resetAccess, dirty };\n})();");
  vm.runInContext(script, context); assert(context.ui);
  const ui = context.ui; ui.applySettings(settings());
  nodes.get("settings-form").querySelectorAll = () => [...nodes.values()].flatMap((item) => item === nodes.get("settings-form") ? [] : item.querySelectorAll());
  t.after(() => { for (const timer of timers) clearTimeout(timer); });
  return { ui, nodes, calls, charts };
}
function input(nodes, wanted) { for (const element of nodes.values()) for (const candidate of element.querySelectorAll()) if (candidate.dataset.path === wanted) return candidate; throw new Error("Missing input " + wanted); }

test("edits a typed profile, keeps other profiles, and saves the server revision", async (t) => {
  let release; const waiting = new Promise((resolve) => { release = resolve; });
  const { ui, nodes, calls } = fixture(t, async ({ body }) => { await waiting; return { ...settings(), revision: 8, settings: JSON.parse(body).settings }; });
  const edit = input(nodes, "profiles.autonomous.medium.model_calls"); edit.value = "16"; edit.listeners.input();
  assert.equal(ui.state.settings.profiles.autonomous.medium.model_calls, 16);
  assert.equal(ui.state.settings.profiles.investigate.medium.model_calls, 8);
  const pending = ui.saveSettings();
  assert.equal(edit.disabled, true);
  await new Promise(setImmediate);
  const mutation = calls.find((item) => item.method === "PUT");
  assert.equal(mutation.headers["X-CSRF-Token"], "test-admin-csrf");
  const submitted = JSON.parse(mutation.body); assert.equal(submitted.revision, 7); assert.equal("revision" in submitted.settings, false);
  release(); await pending;
  assert.equal(ui.state.revision, 8); assert.equal(ui.dirty(), false);
  assert.equal(input(nodes, "profiles.autonomous.medium.model_calls").disabled, false);
});

test("conflicting settings preserve the unsaved draft instead of silently replacing it", async (t) => {
  const { ui, nodes } = fixture(t, () => ({ status: 409, payload: { error: "Changed elsewhere" } }));
  const edit = input(nodes, "autonomy.max_extensions"); edit.value = "9"; edit.listeners.input(); await ui.saveSettings();
  assert.equal(ui.state.settings.autonomy.max_extensions, 9); assert.equal(ui.state.revision, 7); assert.equal(ui.dirty(), true);
  assert.match(nodes.get("error").textContent, /другой сессии/);
});

test("charts use actual aggregates, unknown billed tokens stay unknown, and older active runs remain controllable", async (t) => {
  const { ui, nodes, calls, charts } = fixture(t, ({ route }) => route.endsWith("/cancel") ? { id: "older-active", status: "cancelling" } : metrics());
  ui.renderMetrics(metrics());
  assert.equal(charts.length, 4); assert.equal(charts[2].config.data.datasets[0].data[0], 1.25);
  assert.match(nodes.get("metrics-note").textContent, /API: —/);
  assert.match(nodes.get("active-runs").textContent, /older-acti/);
  assert.match(nodes.get("active-runs").textContent, /Остановить/);
  await ui.cancelRun("older-active");
  const request = calls.find((item) => item.route === "runs/older-active/cancel");
  assert.equal(request.method, "POST"); assert.equal(request.body, "{}");
  assert.equal(request.headers["X-CSRF-Token"], "test-admin-csrf");
});

test("access revoked on the server clears the dashboard and all cached metrics", async (t) => {
  const { ui, nodes, charts } = fixture(t, () => ({ status: 403 }));
  ui.renderMetrics(metrics());
  await ui.refreshMetrics();
  assert.equal(nodes.get("workspace").hidden, true);
  assert.equal(nodes.get("access-panel").hidden, false);
  assert.equal(nodes.get("active-runs").textContent, "");
  assert.equal(ui.state.settings, null);
  assert(charts.every((chart) => chart.destroyed));
});

test("late metrics from an old identity cannot restore private administrative state", async (t) => {
  let release; const pending = new Promise((resolve) => { release = resolve; });
  const { ui, nodes, charts } = fixture(t, async () => { await pending; return metrics(); });
  const loading = ui.refreshMetrics(); ui.resetAccess(); release(); await loading;
  assert.equal(nodes.get("workspace").hidden, true); assert.equal(nodes.get("summary").textContent, ""); assert.equal(charts.length, 0);
});
