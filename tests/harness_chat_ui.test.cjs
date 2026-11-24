// Run with: node --test tests/harness_chat_ui.test.cjs
// Executes the shipped script with a small DOM double; no browser or model required.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const staticPath = path.join(__dirname, "../src/agent_harness/static");
class Node {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.parent = null; this.text = "";
    this.dataset = {}; this.attributes = {}; this.listeners = {};
    this.value = ""; this.hidden = false; this.disabled = false;
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map((child) => child.textContent).join(""); }
  append(...nodes) {
    for (const node of nodes) {
      if (node.tagName === "fragment") { this.append(...[...node.children]); continue; }
      if (node.parent) node.parent.children = node.parent.children.filter((child) => child !== node);
      node.parent = this; this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = []; this.text = ""; this.append(...nodes);
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  querySelectorAll() { return []; }
  querySelector() { return new Node(); }
  focus() {} scrollIntoView() {} close() {} showModal() {}
  get firstElementChild() { return this.children[0]; }
  get lastElementChild() { return this.children.at(-1); }
}

function fixture(t, handler = () => ({}), { session = () => ({ ok: false, status: 404 }), baseURI = "http://localhost/harness/" } = {}) {
  const nodes = new Map([...fs.readFileSync(path.join(staticPath, "index.html"), "utf8")
    .matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Node()]));
  nodes.get("submit-question").append(new Node("span"));
  nodes.get("effort").value = "medium";
  const timers = new Set();
  const document = {
    baseURI, hidden: false,
    getElementById(id) { assert(nodes.has(id), "Missing DOM element: " + id); return nodes.get(id); },
    createElement: (tag) => new Node(tag), createDocumentFragment: () => new Node("fragment"),
    createTextNode(text) { const node = new Node("text"); node.textContent = text; return node; },
    querySelector: () => ({ value: "quick" }), querySelectorAll: () => [], addEventListener() {},
  };
  const window = {
    matchMedia: () => ({ matches: true, addEventListener() {} }), addEventListener() {},
    setTimeout(callback, delay) { const id = setTimeout(callback, delay); timers.add(id); return id; },
    clearTimeout(id) { clearTimeout(id); timers.delete(id); },
  };
  const requests = [];
  const context = vm.createContext({
    document, window, AbortController, URL, console, setTimeout, clearTimeout,
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    async fetch(url, options) {
      const call = { path: url.pathname.replace(new URL("./api/", baseURI).pathname, "") + url.search, ...options };
      requests.push(call);
      const data = await (url.pathname.endsWith("/access/api/session") ? session(call) : handler(call));
      if (typeof data?.ok === "boolean") return { json: async () => ({}), ...data };
      return { ok: true, status: 200, json: async () => data };
    },
  });
  const source = fs.readFileSync(path.join(staticPath, "app.js"), "utf8").replace(
    /  updateSubmit\(\);\s+renderHistory\(\);\s+loadWorkspace\(\);\s+\}\)\(\);\s*$/,
    "globalThis.ui = { state, conversationState, api, loadConversation, loadConversations, " +
    "selectRun, refreshRun, submitRun, resetWorkspace, renderResearchActivity, renderStatus, renderBudget, modelConnectionValue, applyModelConnection, " +
    "settingsState, modelConnectionState, tlsState, changeModelConnection, saveTlsBundle };\n})();",
  );
  vm.runInContext(source, context);
  assert(context.ui, "The test must expose the actual shipped script");
  const ui = context.ui;
  ui.state.status = { enabled: true, indexes: [{ id: "a", name: "Documents" }], model: { available: true } };
  ui.state.selected.add("a");
  t.after(() => { for (const timer of timers) clearTimeout(timer); });
  return { ui, nodes, requests };
}

function run(id, extra = {}) {
  return { id, conversation_id: "chat", question: "Question " + id, answer: "Answer " + id,
    index_ids: ["a"], mode: "quick", effort: "medium", status: "completed", phase: "finished",
    created_at: "2026-11-24T10:00:00Z", evidence: [], warnings: [], metrics: {}, ...extra };
}

test("management link requires explicit server administrator permission", (t) => {
  const { ui, nodes } = fixture(t);
  ui.state.modelDetails = { can_manage: true };
  ui.state.status.can_manage_control = false;
  ui.renderStatus();
  assert.equal(nodes.get("control-open").hidden, true);
  ui.state.status.can_manage_control = true;
  ui.renderStatus();
  assert.equal(nodes.get("control-open").hidden, false);
});

test("opens latest 200 turns, loads older history, and submits after the latest turn", async (t) => {
  const runs = Array.from({ length: 201 }, (_, index) => run(String(index)));
  let submitted;
  const { ui, nodes } = fixture(t, ({ path: route, method, body }) => {
    if (route.startsWith("conversations/chat?")) {
      const query = new URLSearchParams(route.split("?")[1]);
      const offset = Number(query.get("offset")), limit = Number(query.get("limit"));
      return { runs: runs.slice(offset, offset + limit), total_runs: runs.length, offset,
        has_more: offset + limit < runs.length, conversation: { title: "Persistent chat" } };
    }
    if (route === "runs" && method === "POST") { submitted = JSON.parse(body); return run("201"); }
    return {};
  });
  await ui.loadConversation({ id: "chat", title: "Chat" });
  assert.equal(ui.conversationState.offset, 1);
  assert.equal(ui.conversationState.runs.length, 200);
  await ui.loadConversation({ id: "chat" }, true);
  assert.equal(ui.conversationState.runs.length, 201);
  assert.equal(nodes.get("conversation-messages").children.length, 201);
  ui.selectRun(ui.conversationState.runs[0]);
  nodes.get("question").value = "Follow-up";
  await ui.submitRun();
  assert.equal(submitted.parent_run_id, "200");
  assert.equal(ui.state.current.id, "201");
  assert.equal(ui.conversationState.runs.length, 202);
  assert.equal(nodes.get("run-panel").parent.parent.dataset.runId, "201");
});

test("polling updates an active turn while an older answer remains selected", async (t) => {
  const active = run("new", { status: "running", phase: "reading" });
  const { ui, nodes } = fixture(t, ({ path: route }) => route === "runs/new" ? active : route === "runs/old" ? run("old") : {});
  ui.conversationState.id = "chat";
  ui.conversationState.runs = [run("old"), run("new", { status: "running", phase: "planning" })];
  ui.selectRun(ui.conversationState.runs[0]);
  await new Promise(setImmediate);
  await ui.refreshRun("new", ui.state.generation);
  assert.equal(ui.state.current.id, "old");
  assert.equal(ui.conversationState.runs.at(-1).phase, "reading");
  assert.match(nodes.get("conversation-messages").textContent, /Читаем документы/);
  assert.equal(nodes.get("submit-question").disabled, true);
});

test("switching identity prevents a late conversation response from restoring history", async (t) => {
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const { ui } = fixture(t, async () => { await waiting; return { runs: [run("private")], total_runs: 1 }; });
  const loading = ui.loadConversation({ id: "chat" });
  ui.state.identityEpoch += 1;
  ui.resetWorkspace();
  release(); await loading;
  assert.equal(ui.conversationState.id, null);
  assert.equal(ui.conversationState.runs.length, 0);
  assert.equal(ui.state.current, null);
});

test("status uses the current action and does not mislabel planning as source access", (t) => {
  const { ui, nodes } = fixture(t);
  const current = run("1", { status: "running", current_action: { name: "run_in_sandbox" },
    research_trace: [{ name: "update_research_plan", result: "Plan updated" }] });
  ui.renderResearchActivity(current);
  assert.equal(nodes.get("research-current").textContent, "Сейчас: Проверка в песочнице");
  assert.match(nodes.get("research-timeline").textContent, /Обновление плана/);
  ui.renderResearchActivity({ ...current, status: "completed" });
  assert.equal(nodes.get("research-current").hidden, true);
  ui.state.status.identity_scope = "shared";
  ui.renderStatus();
  assert.match(nodes.get("identity-label").textContent, /^Общие чаты/);
});

test("network timeout keeps mutation uncertainty explicit and never retries a POST", async (t) => {
  const { ui, requests } = fixture(t, ({ signal }) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => { const error = new Error("Aborted"); error.name = "AbortError"; reject(error); });
  }));
  await assert.rejects(ui.api("runs", { method: "POST", body: { question: "One request" }, timeoutMs: 5 }),
    (error) => error.name === "RequestTimeoutError" && error.uncertainMutation === true);
  assert.equal(requests.length, 1);
});

test("deepening an autonomous run preserves the mode and selected sandbox", async (t) => {
  let submitted;
  const { ui } = fixture(t, ({ path: route, method, body }) => {
    if (route === "runs" && method === "POST") { submitted = JSON.parse(body); return run("next", { mode: "autonomous" }); }
    return {};
  });
  ui.state.sandboxProfiles = [{ id: "java", name: "Java" }];
  ui.state.selectedSandboxProfile = "java";
  await ui.submitRun(run("first", { mode: "autonomous" }));
  assert.equal(submitted.mode, "autonomous");
  assert.equal(submitted.sandbox_profile_id, "java");
  assert.equal(submitted.parent_run_id, "first");
});

test("budget UI separates main and summary calls and shows actual grants", (t) => {
  const { ui, nodes } = fixture(t);
  ui.renderBudget(run("auto", { mode: "autonomous", metrics: { model_calls: 8, decision_calls: 5, summary_calls: 3 }, budget: { current: { model_calls: 10, summary_calls: 6 }, ceiling: { model_calls: 20, summary_calls: 12 }, events: [{ accepted: true, reason: "Check dependency", requested: { model_calls: 10 }, granted: { model_calls: 10 } }] }, context_summary: { tokens_before: 20000, tokens_after: 3000, compactions: 1, reused_summary: true } }));
  assert.match(nodes.get("run-budget-values").textContent, /Использовано 5/);
  assert.match(nodes.get("run-budget-values").textContent, /Использовано 3/);
  assert.match(nodes.get("run-budget-events").textContent, /Бюджет изменён.*Check dependency/);
  assert.match(nodes.get("run-context-summary").textContent, /сохранённая сводка/);
});

test("API model output limit survives load and save", (t) => {
  const { ui, nodes } = fixture(t);
  ui.applyModelConnection({ provider: "openai", base_url: "https://models.test/v1", output_tokens: 12000 });
  assert.equal(Number(nodes.get("model-output-tokens").value), 12000);
  assert.equal(ui.modelConnectionValue().output_tokens, 12000);
});

test("administrator mutations use the current portal CSRF token without slowing chat requests", async (t) => {
  let token = 0;
  const { ui, requests } = fixture(t, () => ({}), { session: () => ({ csrf_token: "session-" + ++token }) });
  // A personal Bearer token can coexist with the portal's admin cookie.
  ui.state.auth = { kind: "bearer", value: "personal-reader-token" };
  await ui.api("model/connection");
  await ui.api("runs", { method: "POST", body: { question: "Question" } });
  assert.equal(token, 0);
  for (const [path, method] of [["model/connection", "PUT"], ["model/connection/probe", "POST"],
    ["credentials/tls", "POST"], ["credentials/tls/cert", "DELETE"], ["credentials/ssh", "POST"],
    ["settings", "PUT"], ["settings/probe", "POST"], ["model/enable", "POST"]]) {
    await ui.api(path, { method, body: {} });
    const [sessionRequest, mutation] = requests.slice(-2);
    assert.equal(sessionRequest.path, "/access/api/session");
    assert.equal(sessionRequest.headers.Authorization, undefined);
    assert.equal(sessionRequest.credentials, "same-origin");
    assert.equal(mutation.headers["X-CSRF-Token"], "session-" + token);
    assert.equal(mutation.headers.Authorization, "Bearer personal-reader-token");
    assert.equal(mutation.path, path);
    assert.equal(mutation.method, method);
  }
});

test("TLS editor uploads the original certificates after awaiting the admin session", async (t) => {
  let release, uploaded;
  const waiting = new Promise((resolve) => { release = resolve; });
  const { ui, nodes, requests } = fixture(t, ({ path, method, body }) => {
    if (path === "credentials/tls" && method === "POST") uploaded = JSON.parse(body);
    return { credentials: [] };
  }, { session: async () => { await waiting; return { csrf_token: "admin-csrf" }; } });
  ui.settingsState.canManage = true;
  nodes.get("tls-name").value = "Corporate model";
  nodes.get("tls-ca").value = "test-ca-pem";
  nodes.get("tls-cert").value = "test-client-pem";
  nodes.get("tls-key").value = "test-private-key";
  const saving = ui.saveTlsBundle();
  assert.equal(nodes.get("tls-key").value, "");
  assert.equal(uploaded, undefined);
  release(); await saving;
  assert.equal(uploaded.client_key, "test-private-key");
  assert.equal(uploaded.client_certificate, "test-client-pem");
  assert.equal(uploaded.ca_certificate, "test-ca-pem");
  assert.equal(requests.find((request) => request.method === "POST").headers["X-CSRF-Token"], "admin-csrf");
  assert.match(nodes.get("tls-feedback").textContent, /Сертификаты сохранены/);
});

test("model URL and keyless TLS selection survive saving and probing with cookie auth", async (t) => {
  const { ui, requests } = fixture(t, ({ body }) => ({ settings: JSON.parse(body) }),
    { session: () => ({ csrf_token: "admin-csrf" }) });
  ui.applyModelConnection({ provider: "openai", base_url: "https://models.test/v1", tls_bundle_id: "corporate-tls" });
  const config = ui.modelConnectionValue();
  for (const [path, method] of [["model/connection", "PUT"], ["model/connection/probe", "POST"]]) {
    const response = await ui.api(path, { method, body: config });
    assert.equal(response.settings.base_url, "https://models.test/v1");
    assert.equal(response.settings.tls_bundle_id, "corporate-tls");
    assert.equal(response.settings.api_key_env, "");
    assert.equal(requests.at(-1).headers["X-CSRF-Token"], "admin-csrf");
  }
});

test("header authentication still works without a portal session endpoint", async (t) => {
  for (const [kind, status, baseURI] of [["admin", 401, "http://localhost/harness/"],
    ["bearer", 404, "http://localhost/"]]) {
    const { ui, requests } = fixture(t, () => ({ saved: true }), { session: () => ({ ok: false, status }), baseURI });
    ui.state.auth = { kind, value: "administrator-credential" };
    await ui.api("model/connection", { method: "PUT", body: {} });
    assert.equal(requests.at(-1).headers[kind === "admin" ? "X-KB-Admin-Password" : "Authorization"],
      kind === "admin" ? "administrator-credential" : "Bearer administrator-credential");
    assert.equal(requests.at(-1).headers["X-CSRF-Token"], undefined);
  }
});

test("changed identity or cancellation while checking the session never sends credentials", async (t) => {
  for (const changeIdentity of [true, false]) {
    let release;
    const waiting = new Promise((resolve) => { release = resolve; });
    const { ui, requests } = fixture(t, () => ({}), { session: async () => { await waiting; return { csrf_token: "old-session" }; } });
    const controller = new AbortController();
    const saving = ui.api("credentials/tls", { method: "POST", body: { client_key: "private" }, signal: controller.signal });
    if (changeIdentity) ui.state.identityEpoch += 1; else controller.abort();
    release();
    await assert.rejects(saving, { name: "AbortError" });
    assert.equal(requests.length, 1);
  }
});

test("session timeout does not claim that settings may have been saved", async (t) => {
  const { ui, requests } = fixture(t, () => ({}), { session: ({ signal }) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => { const error = new Error("Aborted"); error.name = "AbortError"; reject(error); });
  }) });
  await assert.rejects(ui.api("model/connection", { method: "PUT", body: {}, timeoutMs: 5 }),
    (error) => error.name === "RequestTimeoutError" && error.uncertainMutation === false && /не отправлены/.test(error.message));
  assert.equal(requests.length, 1);
});

test("missing administrator rights give an actionable error and keep unsaved model fields", async (t) => {
  const { ui, nodes, requests } = fixture(t, () => ({ ok: false, status: 403,
    json: async () => ({ error: "Harness administrator access required" }) }),
  { session: () => ({ ok: false, status: 401 }) });
  ui.settingsState.canManage = true;
  ui.applyModelConnection({ provider: "openai", base_url: "https://old.test/v1" });
  nodes.get("direct-model-url").value = "https://new.test/v1";
  await ui.changeModelConnection();
  assert.match(nodes.get("model-connection-feedback").textContent, /Войдите в портал как администратор/);
  assert.equal(nodes.get("direct-model-url").value, "https://new.test/v1");
  assert.equal(ui.modelConnectionState.dirty, true);
  assert.equal(requests.filter((request) => request.method === "PUT").length, 1);
});
