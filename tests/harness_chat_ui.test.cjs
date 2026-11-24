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
    const classes = new Set();
    this.classList = {
      toggle(name, force) { const present = force === undefined ? !classes.has(name) : force; if (present) classes.add(name); else classes.delete(name); return present; },
      add(...names) { for (const name of names) classes.add(name); },
      remove(...names) { for (const name of names) classes.delete(name); },
      contains(name) { return classes.has(name); },
    };
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
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  click() { this.listeners.click?.({ preventDefault() {} }); }
  querySelectorAll() { return []; }
  querySelector() { return new Node(); }
  contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  scrollIntoView() {} close() {} showModal() {}
  get firstElementChild() { return this.children[0]; }
  get lastElementChild() { return this.children.at(-1); }
}

function fixture(t, handler = () => ({}), { session = () => ({ ok: false, status: 404 }), baseURI = "http://localhost/harness/",
  viewportWidth = 1440, storage = new Map(), storageUnavailable = false } = {}) {
  const nodes = new Map([...fs.readFileSync(path.join(staticPath, "index.html"), "utf8")
    .matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Node()]));
  nodes.get("submit-question").append(new Node("span"));
  nodes.get("effort").value = "medium";
  nodes.get("response-mode").value = "quick";
  const timers = new Set();
  const document = {
    baseURI, hidden: false, activeElement: null,
    getElementById(id) { assert(nodes.has(id), "Missing DOM element: " + id); return nodes.get(id); },
    createElement: (tag) => Object.assign(new Node(tag), { ownerDocument: document }), createDocumentFragment: () => new Node("fragment"),
    createTextNode(text) { const node = new Node("text"); node.textContent = text; return node; },
    querySelector: () => ({ value: "quick" }), querySelectorAll: () => [], addEventListener() {},
  };
  for (const node of nodes.values()) node.ownerDocument = document;
  const window = {
    matchMedia: (query) => ({ matches: query.includes("prefers-reduced-motion") ||
      (query.includes("max-width") && viewportWidth <= Number(query.match(/max-width:\s*(\d+)/)[1])), addEventListener() {} }), addEventListener() {},
    setTimeout(callback, delay) { const id = setTimeout(callback, delay); timers.add(id); return id; },
    clearTimeout(id) { clearTimeout(id); timers.delete(id); },
  };
  const requests = [];
  const context = vm.createContext({
    document, window, AbortController, URL, console, setTimeout, clearTimeout,
    localStorage: {
      getItem(key) { if (storageUnavailable) throw new Error("Storage unavailable"); return storage.get(key) ?? null; },
      setItem(key, value) { if (storageUnavailable) throw new Error("Storage unavailable"); storage.set(key, value); },
    },
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
    "selectRun, refreshRun, submitRun, resetWorkspace, loadMemory, renderResearchActivity, renderStatus, renderBudget, modelConnectionValue, applyModelConnection, " +
    "settingsState, modelConnectionState, tlsState, changeModelConnection, saveTlsBundle, layoutState, setPanelCollapsed, initLayout, renderTranscript, renderAnswer, renderRun, applyProfileDefaults, " +
    "normalizedSettings, renderSettings, checkSelectedSandbox, openPersonalCredentials, changePersonalCredential, resetPersonalData };\n})();",
  );
  vm.runInContext(fs.readFileSync(path.join(staticPath, "vendor/markdown-it.min.js"), "utf8"), context);
  context.window.markdownit = context.markdownit;
  vm.runInContext(source, context);
  assert(context.ui, "The test must expose the actual shipped script");
  const ui = context.ui;
  ui.state.status = { enabled: true, indexes: [{ id: "a", name: "Documents" }], model: { available: true } };
  ui.state.selected.add("a");
  t.after(() => { for (const timer of timers) clearTimeout(timer); });
  return { ui, nodes, requests, storage, document };
}

function run(id, extra = {}) {
  return { id, conversation_id: "chat", question: "Question " + id, answer: "Answer " + id,
    index_ids: ["a"], mode: "quick", effort: "medium", status: "completed", phase: "finished",
    created_at: "2026-11-24T10:00:00Z", evidence: [], warnings: [], metrics: {}, ...extra };
}

test("sandbox settings discover images once per effective Docker and preserve separate service ports", async (t) => {
  const { ui, nodes, requests } = fixture(t, (call) => call.path === "settings/sandbox/images" ? {
    images: [{ reference: (JSON.parse(call.body).engine.context || "local") + ":21", size: "250MB" }],
  } : { credentials: [] });
  const config = ui.normalizedSettings({
    sandbox: { enabled: true },
    sandbox_profiles: [{ id: "java", name: "Java", image: "local:21" }, {
      id: "service", name: "Service", image: "other:21", engine: { mode: "context", context: "other" },
      service_port: 8080, service_health_path: "/health",
    }],
  });
  Object.assign(ui.settingsState, { canManage: true, config, activeTab: "sandbox" });
  nodes.get("settings-dialog").open = true;
  ui.renderSettings();
  await new Promise(setImmediate);
  assert.deepEqual(requests.filter((call) => call.path === "settings/sandbox/images")
    .map((call) => JSON.parse(call.body).engine.mode).sort(), ["context", "default"]);
  const [java, service] = nodes.get("settings-sandbox-profiles").children;
  assert.match(java.textContent, /local:21 · 250MB/);
  assert.doesNotMatch(java.textContent, /other:21/);
  assert.match(service.textContent, /other:21 · 250MB/);
  assert.equal(config.sandbox_profiles[0].engine, null);
  assert.equal(config.sandbox_profiles[1].service_port, 8080);
  assert.equal(config.sandbox_profiles[1].engine.port, 2376);
  assert.equal(config.sandbox_profiles[1].service_health_path, "/health");
});

test("research can use the selected sandbox HTTP API without any RAG index or another profile", async (t) => {
  const { ui, nodes, requests } = fixture(t, (call) =>
    call.path === "sandbox/check" ? { available: true, message: "GET /ready вернул HTTP 204." } : run("answer"));
  nodes.get("response-mode").value = "investigate";
  Object.assign(ui.state, { sandboxProfiles: [{ id: "java", name: "Java" }], selectedSandboxProfile: "java" });
  ui.state.status.indexes = [{ id: "sandbox-api:~" }, { id: "sandbox-api:java" }];
  ui.state.selected.clear();
  await ui.checkSelectedSandbox();
  assert.equal(JSON.parse(requests.find((r) => r.path === "sandbox/check").body).profile_id, "java");
  assert.match(nodes.get("sandbox-check-result").textContent, /HTTP 204/);
  nodes.get("question").value = "Проверь сервис";
  await ui.submitRun();
  assert.deepEqual(JSON.parse(requests.find((r) => r.path === "runs").body).index_ids, ["sandbox-api:java"]);
});

test("credentials requested in a chat use a private editor and bypass admin, runs and browser storage", async (t) => {
  const repository = { id: "proj", name: "Проект", ssh: true };
  const { ui, nodes, requests, storage } = fixture(t, (call) => call.method === "GET"
    ? { credentials: [], repositories: [repository] } : { credential: { kind: "gradle", id: "personal-1", repository_id: "proj", name: "Сборка" } });
  nodes.get("question").value = "Запомни креды Gradle";
  await ui.submitRun(); await new Promise(setImmediate);
  assert.equal(nodes.get("question").value, "");
  assert.equal(nodes.get("personal-kind").value, "gradle");
  assert.equal(nodes.get("personal-repository").value, "proj");
  assert.equal(nodes.get("personal-save").disabled, false);
  nodes.get("personal-name").value = "Сборка"; nodes.get("personal-registry").value = "https://packages.example/maven";
  nodes.get("personal-username").value = "user"; nodes.get("personal-password").value = "test-private-value";
  await ui.changePersonalCredential();
  assert.equal(nodes.get("personal-password").value, "");
  assert.doesNotMatch(nodes.get("personal-list").textContent, /test-private-value/);
  assert.deepEqual(requests.map((r) => [r.method, r.path]), [["GET", "credentials/personal"], ["POST", "credentials/personal"]]);
  assert.equal(JSON.parse(requests[1].body).kind, "gradle");
  assert.doesNotMatch(JSON.stringify([...storage]), /test-private-value/);
  await ui.changePersonalCredential("personal-1");
  assert.equal(requests.at(-1).path, "credentials/personal/personal-1");
  assert.equal(nodes.get("personal-list").children.length, 0);
});

test("pasting a private key into chat diverts it to the SSH form and clears it on close", async (t) => {
  const { nodes, requests } = fixture(t, () => ({ repositories: [{ id: "project", ssh: true }] }));
  let prevented = false;
  const key = "-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n-----END OPENSSH PRIVATE KEY-----";
  nodes.get("question").value = "До вставки";
  nodes.get("question").listeners.paste({ clipboardData: { getData: () => key }, preventDefault() { prevented = true; } });
  await new Promise(setImmediate);
  assert.equal(prevented, true);
  assert.equal(nodes.get("question").value, "До вставки");
  assert.equal(nodes.get("personal-key").value, key);
  nodes.get("personal-dialog").listeners.close();
  assert.equal(nodes.get("personal-key").value, "");
  assert.doesNotMatch(JSON.stringify(requests), /BEGIN OPENSSH/);
});

test("panels collapse independently without losing the message draft or leaving hidden keyboard focus", (t) => {
  const { ui, nodes, document } = fixture(t);
  nodes.get("question").value = "My unfinished follow-up";
  nodes.get("history-content").append(nodes.get("new-run"));
  nodes.get("new-run").focus();
  ui.setPanelCollapsed("history", true);
  assert.equal(nodes.get("history-content").hidden, true);
  assert.equal(nodes.get("history-toggle").attributes["aria-expanded"], "false");
  assert.equal(nodes.get("history-toggle").attributes["aria-label"], "Показать список чатов");
  assert.equal(document.activeElement, nodes.get("history-toggle"));
  assert.equal(nodes.get("workspace").classList.contains("history-collapsed"), true);
  assert.equal(nodes.get("context-content").hidden, false);
  nodes.get("context-toggle").listeners.click();
  assert.equal(nodes.get("context-content").hidden, true);
  assert.equal(nodes.get("context-toggle").attributes["aria-expanded"], "false");
  assert.equal(nodes.get("context-toggle").attributes["aria-label"], "Показать источники");
  nodes.get("context-toggle").listeners.click();
  assert.equal(nodes.get("context-content").hidden, false);
  assert.equal(nodes.get("context-toggle").attributes["aria-expanded"], "true");
  assert.equal(nodes.get("context-toggle").attributes["aria-label"], "Свернуть источники");
  assert.equal(nodes.get("history-content").hidden, true);
  nodes.get("history-toggle").listeners.click();
  assert.equal(nodes.get("history-content").hidden, false);
  assert.equal(nodes.get("history-toggle").attributes["aria-expanded"], "true");
  assert.equal(nodes.get("question").value, "My unfinished follow-up");
});

test("panel defaults leave room for the chat on narrow screens and remain reversible", (t) => {
  const tablet = fixture(t, undefined, { viewportWidth: 881 });
  assert.equal(tablet.nodes.get("history-content").hidden, false);
  assert.equal(tablet.nodes.get("context-content").hidden, true);
  const phone = fixture(t, undefined, { viewportWidth: 390 });
  assert.equal(phone.nodes.get("history-content").hidden, true);
  assert.equal(phone.nodes.get("context-content").hidden, true);
  phone.nodes.get("context-shortcut").listeners.click();
  assert.equal(phone.nodes.get("context-content").hidden, false);
  assert.equal(phone.nodes.get("history-content").hidden, true);
});

test("explicit panel preferences survive reload even when the viewport changes", (t) => {
  const first = fixture(t, undefined, { viewportWidth: 1440 });
  first.ui.setPanelCollapsed("history", true);
  first.ui.setPanelCollapsed("context", false);
  const restored = fixture(t, undefined, { viewportWidth: 881, storage: first.storage });
  assert.equal(restored.nodes.get("history-content").hidden, true);
  assert.equal(restored.nodes.get("context-content").hidden, false);
  assert.equal(restored.nodes.get("history-toggle").attributes["aria-expanded"], "false");
  assert.equal(restored.nodes.get("context-toggle").attributes["aria-expanded"], "true");
});

test("blocked or malformed preference storage never disables panel controls or chat requests", async (t) => {
  for (const options of [{ storageUnavailable: true }, { storage: new Map([["agent-harness.layout.v1", "{broken"]]) }]) {
    const { ui, nodes } = fixture(t, () => ({ accepted: true }), options);
    ui.setPanelCollapsed("history", true);
    ui.setPanelCollapsed("context", true);
    nodes.get("history-toggle").listeners.click();
    assert.equal(nodes.get("history-content").hidden, false);
    assert.equal(nodes.get("context-content").hidden, true);
    assert.equal((await ui.api("runs", { method: "POST", body: { question: "Follow-up" } })).accepted, true);
  }
});

test("transcript updates preserve older reading position and follow new content only from the bottom", (t) => {
  const { ui, nodes } = fixture(t);
  const scroll = nodes.get("chat-scroll"), messages = nodes.get("conversation-messages");
  scroll.scrollHeight = 2000; scroll.clientHeight = 500; scroll.scrollTop = 240;
  scroll.scrollTo = ({ top }) => { scroll.scrollTop = Math.min(top, scroll.scrollHeight - scroll.clientHeight); };
  const replace = messages.replaceChildren.bind(messages);
  messages.replaceChildren = (...items) => {
    replace(...items);
    // Rebuilding the message tree can temporarily reset browser scroll anchoring.
    scroll.scrollTop = 0; scroll.scrollHeight += 400;
  };
  ui.conversationState.id = "chat";
  ui.conversationState.runs = [run("old"), run("new", { status: "running", answer: "Partial answer" })];
  ui.renderTranscript();
  assert.equal(scroll.scrollTop, 240);
  scroll.scrollTop = scroll.scrollHeight - scroll.clientHeight;
  ui.conversationState.runs.at(-1).answer += " with more evidence";
  ui.renderTranscript();
  assert.equal(scroll.scrollTop, scroll.scrollHeight - scroll.clientHeight);
});

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

test("composer offers three answer modes, selects read-only MCP for quick replies and preserves other sources", async (t) => {
  const html = fs.readFileSync(path.join(staticPath, "index.html"), "utf8");
  const composer = html.match(/<form id="question-form"[^>]*>([\s\S]*?)<\/form>/)[1];
  assert(composer.indexOf("</details>") < composer.indexOf('id="response-mode"'));
  const options = composer.match(/<select id="response-mode"[^>]*>([\s\S]+?)<\/select>/)[1];
  assert.deepEqual([...options.matchAll(/value="(\w+)"/g)].map((match) => match[1]), ["quick", "investigate", "autonomous"]);
  let submitted;
  const { ui, nodes } = fixture(t, ({ path: route, method, body }) => {
    if (route === "runs" && method === "POST") submitted = JSON.parse(body);
    return run("found", { ...submitted });
  });
  const choose = (value) => { nodes.get("response-mode").value = value; nodes.get("response-mode").listeners.change(); };
  ui.state.status.indexes = [
    { id: "wiki", direct_search: true }, { id: "graph:system", quick_search: true },
    { id: "connection:jira", direct_search: false, quick_search: true },
    { id: "connection:agent", quick_search: false }, { id: "repo:billing", quick_search: false },
  ];
  ui.state.selectedSandboxProfile = "unavailable";
  ui.renderStatus();
  choose("quick");
  assert.equal(nodes.get("submit-question").firstElementChild.textContent, "Отправить");
  assert.equal(nodes.get("composer-environment").hidden, true);
  assert.deepEqual(nodes.get("source-list").children.map((row) => row.firstElementChild.value), ["wiki", "graph:system", "connection:jira"]);
  nodes.get("select-all-sources").checked = false; nodes.get("select-all-sources").listeners.change();
  assert.equal(nodes.get("source-count").textContent, "0 / 3");
  choose("investigate");
  assert.equal(nodes.get("source-count").textContent, "2 / 5");
  assert.equal(nodes.get("composer-environment").hidden, false);
  choose("quick");
  nodes.get("question").value = "В каком статусе PAY-42?";
  nodes.get("question-form").listeners.submit({ preventDefault() {} });
  assert.equal(submitted, undefined);
  assert.match(nodes.get("error-banner").textContent, /MCP/);
  const jira = nodes.get("source-list").children[2].firstElementChild;
  jira.checked = true; jira.listeners.change();
  nodes.get("question-form").listeners.submit({ preventDefault() {} });
  await new Promise(setImmediate);
  assert.equal(submitted.mode, "quick");
  assert.deepEqual(submitted.index_ids, ["connection:jira"]);
  assert.equal(submitted.sandbox_profile_id, "");
});

test("a disconnected model shows a connection warning and keeps the selected answer mode", (t) => {
  const { ui, nodes } = fixture(t);
  nodes.get("response-mode").value = "investigate";
  ui.state.status.model = { available: false, state: "disabled" };
  ui.renderStatus();
  assert.equal(nodes.get("notice").hidden, false);
  assert.match(nodes.get("notice").textContent, /подключение/);
  assert.match(nodes.get("connection-state").className, /warning/);
  assert.equal(nodes.get("response-mode").value, "investigate");
});

test("a model asking for a missing identifier keeps the chat open for a follow-up", (t) => {
  const { ui, nodes } = fixture(t);
  const clarification = run("first", { status: "partial", stop_reason: "no_sources", answer_kind: "clarification",
    answer: "Какой номер у обращения в Jira?" });
  ui.conversationState.id = "chat"; ui.conversationState.runs = [clarification]; ui.state.current = clarification;
  ui.renderRun();
  assert.match(nodes.get("conversation-messages").textContent, /Уточнение Harness/);
  assert.equal(nodes.get("run-status").textContent, "Ждём уточнения");
  assert.equal(nodes.get("answer").textContent, "Какой номер у обращения в Jira?");
  assert.equal(nodes.get("submit-question").disabled, false);
});

test("opening an archived search chat preserves its results and uses the model for its follow-up", async (t) => {
  const last = run("searched", { mode: "search", effort: "low", resumable: true });
  let submitted;
  const { ui, nodes } = fixture(t, ({ path: route, method, body }) => {
    if (route.startsWith("conversations/chat?")) return { runs: [last], total_runs: 1 };
    if (route === "runs" && method === "POST") submitted = JSON.parse(body);
    return run("next", { ...submitted });
  });
  nodes.get("response-mode").value = "autonomous";
  await ui.loadConversation({ id: "chat" });
  assert.match(nodes.get("conversation-messages").textContent, /Результаты поиска/);
  assert.equal(nodes.get("response-mode").value, "quick");
  assert.equal(nodes.get("resume-run").hidden, true);
  assert.equal(nodes.get("effort").value, "low");
  nodes.get("question").value = "Где лимиты?";
  await ui.submitRun();
  assert.equal(submitted.mode, "quick");
  assert.equal(submitted.parent_run_id, "searched");
  assert.equal(submitted.effort, "low");
});

test("opening sources on an older answer selects its materials and expands the panel without starting a search", async (t) => {
  const old = run("old", { evidence: [{ id: "s1", index_id: "a", title: "Путь платежа", text: "Маршрут A" }] });
  const latest = run("latest", { mode: "investigate", answer_kind: "search_results",
    evidence: [{ id: "s2", index_id: "a", title: "Другая запись", text: "Маршрут B" }] });
  const { ui, nodes, document, requests } = fixture(t, ({ path: route }) => route === "runs/old" ? old : latest);
  ui.conversationState.id = "chat"; ui.conversationState.runs = [old, latest];
  ui.state.current = latest; ui.setPanelCollapsed("context", true); ui.renderRun();
  const [previousTurn, lastTurn] = nodes.get("conversation-messages").children;
  assert.match(lastTurn.textContent, /Результаты поиска/);
  assert.doesNotMatch(lastTurn.textContent, /Источники ·/);
  const button = previousTurn.children[1].children[0].children.find((item) => item.attributes["aria-controls"] === "evidence-list");
  assert.equal(button.textContent, "Источники · 1");
  button.click(); await new Promise(setImmediate);
  assert.equal(ui.state.current.id, "old");
  assert.equal(nodes.get("context-content").hidden, false);
  assert.equal(document.activeElement, nodes.get("evidence-list"));
  assert.match(nodes.get("evidence-list").textContent, /Маршрут A/);
  assert.doesNotMatch(nodes.get("evidence-list").textContent, /Маршрут B/);
  assert(!requests.some((item) => item.method === "POST"));
});

test("long source IDs become links to their own cards and quoted or unknown IDs remain literal", (t) => {
  const { ui, nodes, document } = fixture(t);
  const hash = "a".repeat(64), missing = "b".repeat(64);
  const result = run("one", { mode: "search", answer_kind: "search_results",
    answer: `**Результаты поиска**\n\n**Заголовок**\n\nАС [${hash}]\n\n> Образец [${hash}]\n\nАС [${missing}]`,
    evidence: [{ id: "other", index_id: "a", text: "Другая запись" }, { id: hash, index_id: "a", text: "Образец" }] });
  ui.conversationState.id = "chat"; ui.conversationState.runs = [result];
  ui.state.current = result; ui.setPanelCollapsed("context", true); ui.renderRun();
  const content = nodes.get("answer");
  const link = content.children[1].children.find((node) => node.tagName === "button");
  assert.equal(link.textContent, "[2]"); assert.equal(link.title, hash);
  assert(content.textContent.includes("Образец [" + hash + "]"));
  assert(content.textContent.includes(missing)); assert(!content.textContent.includes("Результаты поиска"));
  link.click();
  assert.equal(document.activeElement, nodes.get("evidence-list").children[1]);
  assert.equal(nodes.get("context-content").hidden, false);
});

test("model answers link their literal source IDs and numbered references to this turn's evidence", (t) => {
  const { ui, nodes, document } = fixture(t);
  const hash = "f".repeat(64);
  const result = run("model-answer", {
    answer: "Сумма проверена [" + hash + "]. Второй источник [2]. Настройка `items[2]`, [см. [2]](https://example.org/) и [unknown].",
    evidence: [{ id: hash, index_id: "a", text: "Предел" }, { id: "doc", index_id: "a", text: "Сумма" }],
  });
  ui.conversationState.id = "chat"; ui.conversationState.runs = [result]; ui.state.current = result;
  ui.renderRun();
  const paragraph = nodes.get("answer").firstElementChild;
  const citations = paragraph.children.filter((node) => node.tagName === "button");
  assert.deepEqual(citations.map((node) => node.textContent), ["[1]", "[2]"]);
  assert.deepEqual(citations.map((node) => node.title), [hash, "doc"]);
  assert.match(paragraph.textContent, /items\[2\].*см\. \[2\].*\[unknown\]/);
  citations[1].click();
  assert.equal(document.activeElement, nodes.get("evidence-list").children[1]);
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

test("Autopilot probe tests unsaved direct settings and keeps the simple probe available", async (t) => {
  let finishProbe;
  const probeResult = new Promise((resolve) => { finishProbe = resolve; });
  const { ui, nodes, requests } = fixture(t, (request) => request.path.includes("?autopilot=") ? probeResult :
    { available: true, message: "Модель ответила" }, { session: () => ({ csrf_token: "session-token" }) });
  ui.settingsState.canManage = true;
  ui.applyModelConnection({ provider: "gigacode" });
  const probe = nodes.get("model-autopilot-probe");
  assert.equal(probe.hidden, false);
  assert.equal(probe.disabled, false);
  ui.applyModelConnection({ provider: "openai", base_url: "https://saved.test/v1" });
  nodes.get("direct-model-url").value = "https://draft.test/v1";
  nodes.get("direct-model-tls").value = "corp";
  assert.equal(probe.hidden, false);
  const pending = probe.listeners.click();
  assert.match(nodes.get("model-connection-feedback").textContent, /Проверяем ответ и выбор инструмента/);
  assert(probe.disabled && nodes.get("model-connection-probe").disabled);
  finishProbe({ available: false, message: "Модель не вызвала инструмент." });
  await pending;
  assert.match(nodes.get("model-connection-feedback").textContent, /Модель не вызвала инструмент.*Настройки ещё не сохранены/);
  await nodes.get("model-connection-probe").listeners.click();
  const calls = requests.filter((request) => request.method === "POST");
  assert.deepEqual(calls.map((call) => call.path), ["model/connection/probe?autopilot=1", "model/connection/probe"]);
  assert.equal(calls[0].body, calls[1].body);
  assert.equal(JSON.parse(calls[0].body).base_url, "https://draft.test/v1");
  assert.equal(JSON.parse(calls[0].body).tls_bundle_id, "corp");
  assert(!requests.some((call) => call.method === "PUT"));
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

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}

test("source history groups only the observed outcomes of that search", async (t) => {
  const { ui, nodes } = fixture(t, () => ({ memories: [{ id: "sample", text: "", index_ids: ["a", "b", "c", "d"],
    strategy: { source_outcomes: { a: "useful", b: "empty", c: "irrelevant", d: "empty", other: "irrelevant" } } }] }));
  ui.state.status.indexes.push({ id: "b", name: "Архив" }, { id: "c", name: "Тикеты" }, { id: "d", name: "Код" });
  await ui.loadMemory();
  const section = descendants(nodes.get("memory-list")).find((node) => node.tagName === "details");
  assert.equal(section.firstElementChild.textContent, "Что показал прошлый поиск по источникам");
  assert.deepEqual(section.lastElementChild.children.map((row) => [row.text, row.lastElementChild.textContent]), [
    ["Материалы использованы в ответе", "Documents"],
    ["Поиск не вернул материалов", "Архив, Код"],
    ["Просмотренные фрагменты не ответили на вопрос", "Тикеты"],
  ]);
});

test("shared memory distinguishes cited tool results, incomplete checks and failed calls", async (t) => {
  const base = { kind: "case", status: "experience", visibility: "shared", index_ids: ["a"], text: "Материалы проверки" };
  const success = { tool: "mcp_call", index_id: "a", remote_tool: "jira_get_issue", evidence_ids: ["PAY-72"],
    args: { token: "PRIVATE_ARGUMENT" } };
  const { ui, nodes } = fixture(t, () => ({ memories: [
    { ...base, id: "case", title: "Платежи", strategy: { successful_tools: [success] },
      failures: [{ operation: "external", remote_tool: "jira_history", tool: "mcp_call", index_ids: ["a"], reason: "PRIVATE_ERROR" }] },
    { ...base, id: "unfinished", title: "Поиск причины", outcome: "partial",
      strategy: { observed_tools: [{ tool: "read_document", index_id: "a", evidence_ids: ["REG-4"] }] } },
    { ...base, id: "old", title: "Прежняя запись", strategy: { tools: ["model_response", "mcp_call"] } },
    { ...base, id: "old-failure", title: "Старый сбой", failures: [{ operation: "external", index_ids: ["a"] }] },
  ] }));
  await ui.loadMemory();
  const [current, unresolved, historical, legacyFailure] = nodes.get("memory-list").children;
  const [helpful, failed] = current.children.filter((node) => node.tagName === "details");
  assert.equal(helpful.firstElementChild.textContent, "Что помогло · jira_get_issue · Documents");
  assert.match(helpful.textContent, /PAY-72/);
  assert.equal(failed.firstElementChild.textContent, "Сбои инструментов · jira_history · Documents");
  assert.doesNotMatch(current.textContent, /PRIVATE_/);
  assert.match(unresolved.textContent, /Материалы незавершённой проверки · Чтение документа · Documents/);
  assert.match(unresolved.textContent, /REG-4/);
  assert.doesNotMatch(unresolved.textContent, /Что помогло/);
  assert(!historical.children.some((node) => node.tagName === "details"));
  assert.match(legacyFailure.textContent, /Сбои инструментов · Внешний инструмент · Documents/);
  ui.state.current = run("with-hint", { memory_used: [{ title: "Платежи", successful_tools: [success] }] });
  ui.renderRun();
  assert.match(nodes.get("run-memory-used").textContent, /Предложено агенту из опыта · jira_get_issue · Documents/);
  assert.doesNotMatch(nodes.get("run-memory-used").textContent, /PRIVATE_|PAY-72/);
});

test("shared memory names known failure categories and explains why a run was not saved", async (t) => {
  const { ui, nodes } = fixture(t, () => ({ memories: [{ id: "failed", text: "", index_ids: ["a"],
    failures: ["timeout", "permission", "remote_error", "nonzero_exit", "PRIVATE_ERROR"].map((reason) => ({
      operation: "external", index_ids: ["a"], reason,
    })) }] }));
  await ui.loadMemory();
  const failures = nodes.get("memory-list").textContent;
  for (const label of ["Истекло время ожидания", "Доступ к источнику отклонён", "Внешний инструмент сообщил об ошибке", "Команда завершилась с ошибкой"]) {
    assert(failures.includes(label));
  }
  assert.doesNotMatch(failures, /PRIVATE_ERROR/);
  for (const [reason, expected] of [["full", /достигнут лимит записей/], ["disabled", /отключена в ваших настройках памяти/]]) {
    ui.state.current = run(reason, { memory_contribution: { updated: false, reason },
      research_trace: [{ name: "memory_unavailable", result: "Сохранить запись не удалось." }] });
    ui.renderRun();
    assert.equal(nodes.get("run-memory-used").hidden, false);
    assert.match(nodes.get("run-memory-used").textContent, expected);
    assert.doesNotMatch(nodes.get("run-memory-used").textContent, /добавлен/);
    assert.match(nodes.get("research-timeline").textContent, /Общая память временно недоступна/);
  }
});

test("Markdown preserves heading levels, nested lists, tables, quotations and fenced code", (t) => {
  const { ui, nodes } = fixture(t);
  ui.renderAnswer("## Вывод\n\n**Оплата** прошла, *уведомление* задержалось.\n\n1. Проверить очередь\n   - Найти событие\n   - Сверить ID\n2. Повторить доставку\n\n> Проверено по журналу\n\n| Сервис | Статус |\n| --- | ---: |\n| API | Готово |\n\n```java\nSystem.out.println(\"ok\");\n```\n");
  const tree = descendants(nodes.get("answer"));
  for (const tag of ["h2", "strong", "em", "ol", "ul", "blockquote", "table", "thead", "tbody", "th", "td", "pre", "code"]) {
    assert(tree.some((node) => node.tagName === tag), "Expected Markdown element: " + tag);
  }
  const nested = tree.find((node) => node.tagName === "ul");
  assert.equal(nested.parent.tagName, "li");
  assert.equal(tree.find((node) => node.tagName === "pre").textContent, 'System.out.println("ok");');
  assert.equal(tree.find((node) => node.tagName === "table").parent.attributes["role"], "region");
});

test("untrusted answers cannot execute HTML, create unsafe links or load remote images", (t) => {
  const { ui, nodes } = fixture(t);
  ui.renderAnswer('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[unsafe](javascript:alert(1))\n\n![external](https://example.test/tracking.png)\n\n[safe](https://example.test/doc)');
  const tree = descendants(nodes.get("answer"));
  assert(!tree.some((node) => ["script", "img", "iframe"].includes(node.tagName)));
  const links = tree.filter((node) => node.tagName === "a");
  assert.equal(links.length, 2);
  assert(links.every((node) => node.href.startsWith("https://")));
  assert(links.every((node) => node.rel.includes("noopener")));
  assert.match(nodes.get("answer").textContent, /<script>alert/);
});

test("chat separates user's question and Harness answer without duplicating technical status", (t) => {
  const { ui, nodes } = fixture(t);
  const first = run("first");
  ui.conversationState.id = "chat"; ui.conversationState.runs = [first];
  ui.renderTranscript();
  const turn = nodes.get("conversation-messages").children[0];
  assert.equal(turn.children[0].children[0].textContent, "Вы");
  assert.equal(turn.children[1].children[0].children[0].textContent, "Ответ Harness");
  assert.equal(turn.children[1].children[1].textContent, "Answer first");
});

test("live activity shows model waiting and elapsed time, then collapses completed investigation", (t) => {
  const { ui, nodes } = fixture(t);
  ui.conversationState.id = "chat";
  const active = run("active", { status: "running", phase: "synthesizing", answer: "", current_action: { name: "model_response", started_at: new Date(Date.now() - 125000).toISOString() }, research_trace: [{ name: "mcp_list_tools", result: "Найдено 6 инструментов" }] });
  ui.state.current = active; ui.conversationState.runs = [active]; ui.renderRun();
  assert.equal(nodes.get("run-progress").hidden, false);
  assert.match(nodes.get("run-status").textContent, /Модель анализирует/);
  assert.match(nodes.get("run-waiting").textContent, /2 мин 5 сек/);
  assert.match(nodes.get("run-last-step").textContent, /Изучаем доступные инструменты/);
  nodes.get("run-details").open = true;
  ui.renderRun();
  assert.equal(nodes.get("run-details").open, true, "Polling must not undo an explicit disclosure choice");
  const finished = run("active"); ui.state.current = finished; ui.conversationState.runs = [finished]; ui.renderRun();
  assert.equal(nodes.get("run-progress").hidden, true);
  assert.equal(nodes.get("run-details").open, false);
  assert.equal(nodes.get("answer").textContent, "Answer active");
});

test("Autopilot shows its next attempt when a gateway delays a request, and Stop cancels the wait", async (t) => {
  const next = Math.ceil(Date.now() / 1000) + 120;
  const waiting = run("auto", { mode: "autonomous", status: "queued", phase: "waiting_for_gateway",
    answer: "", current_action: { name: "model_retry", retry_at: next } });
  const stopped = { ...waiting, status: "cancelled", phase: "finished", current_action: null };
  const { ui, nodes, requests } = fixture(t, () => stopped);
  ui.conversationState.id = "chat"; ui.conversationState.runs = [waiting]; ui.state.current = waiting;
  ui.renderRun();
  assert.equal(nodes.get("run-waiting").hidden, false);
  assert.match(nodes.get("run-status").textContent, /Ожидаем шлюз модели/);
  assert.match(nodes.get("run-phase").textContent, /отложить запрос/);
  assert(nodes.get("run-waiting").textContent.includes(new Date(next * 1000).toLocaleTimeString("ru-RU")));
  assert.equal(nodes.get("cancel-run").hidden, false);
  await nodes.get("cancel-run").listeners.click();
  assert(requests.some((call) => call.path === "runs/auto/cancel" && call.method === "POST"));
  assert.equal(nodes.get("run-waiting").hidden, true);
  assert.equal(nodes.get("cancel-run").hidden, true);
});

test("one poll shows why research continued, the active target, and earlier steps from the server", async (t) => {
  const trace = [
    { name: "mcp_list_tools", result: "Изучен каталог" },
    ...Array.from({ length: 20 }, () => ({ name: "research_round", result: "Проверены материалы" })),
    { name: "model_planning" }, { name: "answer_quality_rejected", result: "В ответе отсутствуют ссылки на источники." },
    { name: "research_recovery", result: "Проверяем другой документ" },
  ];
  const active = run("auto", { status: "running", mode: "autonomous", phase: "reading", answer: "",
    research_trace: trace, current_action: { name: "read_document", args: { index_id: "a", document_id: "PAY-42" } } });
  const { ui, nodes } = fixture(t, ({ path }) => path === "runs/auto" ? active : {});
  ui.state.current = run("auto", { status: "queued" });
  ui.conversationState.id = "chat"; ui.conversationState.runs = [ui.state.current];
  await ui.refreshRun("auto", ui.state.generation);
  assert.equal(nodes.get("run-recovery-note").hidden, false);
  assert.match(nodes.get("run-recovery-note").textContent, /Черновик не прошёл проверку.*отсутствуют ссылки/);
  assert.match(nodes.get("run-status").textContent, /Чтение документа · Documents · PAY-42/);
  assert.match(nodes.get("run-last-step").textContent, /Поиск дополнительных подтверждений/);
  const timeline = nodes.get("research-timeline");
  assert.equal(timeline.children.length, trace.length);
  assert.match(timeline.firstElementChild.textContent, /другой документ/);
  assert.match(timeline.lastElementChild.textContent, /Изучен каталог/);
  ui.state.current = { ...active, current_action: null, status: "partial", stop_reason: "model_policy_stop" };
  ui.renderRun();
  assert.equal(nodes.get("run-recovery-note").hidden, true);
  assert.equal(nodes.get("run-status").textContent, "Провайдер остановил генерацию");
});

test("DeepSeek profile recommendations only change custom values after the apply action", (t) => {
  const { ui, nodes } = fixture(t);
  ui.applyModelConnection({ provider: "openai", model: "deepseek-v4-flash", profile: "deepseek_v4_flash", timeout_seconds: 900, output_tokens: 12000 }, { profiles: [{ id: "deepseek_v4_flash", defaults: { timeout_seconds: 1800, output_tokens: 32768, model_window_tokens: 256000 } }] });
  assert.equal(ui.modelConnectionValue().timeout_seconds, 900);
  assert.equal(ui.modelConnectionValue().output_tokens, 12000);
  assert.equal(ui.modelConnectionValue().profile, "deepseek_v4_flash");
  ui.applyProfileDefaults();
  assert.equal(ui.modelConnectionValue().timeout_seconds, 1800);
  assert.equal(ui.modelConnectionValue().output_tokens, 32768);
  assert.equal(ui.modelConnectionState.dirty, true);
  assert.match(nodes.get("model-connection-feedback").textContent, /Сохраните подключение/);
});


test("switching from untouched generic defaults to DeepSeek adopts its recommended limits", (t) => {
  const { ui, nodes } = fixture(t);
  ui.applyModelConnection({ provider: "openai", profile: "auto", model: "", timeout_seconds: 60, output_tokens: 4096 }, { resolved_profile: "generic", profile_defaults: { timeout_seconds: 60, output_tokens: 4096, model_window_tokens: 65536 } });
  nodes.get("direct-model-name").value = "deepseek-v4-flash";
  nodes.get("model-profile").value = "deepseek_v4_flash";
  nodes.get("model-profile").listeners.change();
  assert.equal(ui.modelConnectionValue().timeout_seconds, 1800);
  assert.equal(ui.modelConnectionValue().output_tokens, 32768);
  assert.equal(ui.modelConnectionState.dirty, true);
  nodes.get("model-profile").value = "generic";
  nodes.get("model-profile").listeners.change();
  assert.equal(ui.modelConnectionValue().timeout_seconds, 60);
  assert.equal(ui.modelConnectionValue().output_tokens, 4096);
});

test("profile changes preserve custom limits and only update untouched default fields", (t) => {
  const { ui, nodes } = fixture(t);
  ui.applyModelConnection({ provider: "openai", profile: "generic", timeout_seconds: 120, output_tokens: 8192 });
  nodes.get("model-profile").value = "deepseek_v4_flash";
  nodes.get("model-profile").listeners.change();
  assert.equal(ui.modelConnectionValue().timeout_seconds, 120);
  assert.equal(ui.modelConnectionValue().output_tokens, 8192);
  ui.applyModelConnection({ provider: "openai", profile: "generic", timeout_seconds: 120, output_tokens: 4096 });
  nodes.get("model-profile").value = "deepseek_v4_flash";
  nodes.get("model-profile").listeners.change();
  assert.equal(ui.modelConnectionValue().timeout_seconds, 120);
  assert.equal(ui.modelConnectionValue().output_tokens, 32768);
});
