"use strict";

(() => {
  const AUTH_KEY = "agent-harness.auth.v1";
  const LAYOUT_KEY = "agent-harness.layout.v1";
  const ACTIVE = new Set(["queued", "running", "cancelling"]);
  const MODEL_PENDING = new Set(["starting", "authentication_required"]);
  const SETTINGS_TABS = ["repositories", "connections", "sandbox", "ssh", "discoveries", "model"];
  const STATUS_LABELS = {
    queued: "Запрос принят", running: "Работаем над запросом", completed: "Готово",
    partial: "Собраны доступные сведения", cancelling: "Останавливаем работу", cancelled: "Остановлено", timed_out: "Время работы истекло",
    failed: "Не удалось завершить", interrupted: "Работа была прервана",
  };
  const PHASE_LABELS = {
    queued: "Ожидаем свободное место для запуска.", searching: "Ищем подтверждения в выбранных источниках.",
    reading: "Читаем документы и проверяем подробности за пределами поисковых фрагментов.",
    planning: "Определяем, какие сведения нужны для ответа.",
    refining: "Уточняем запросы и проверяем оставшиеся вопросы.",
    reading_code: "Изучаем код подключённых сервисов.",
    running_code: "Проверяем код в песочнице.",
    checking: "Проверяем гипотезы по найденным материалам.",
    compacting: "Сохраняем суть беседы и готовим контекст.",
    budget_review: "Проверяем, сколько дополнительных ресурсов нужно исследованию.",
    authentication_required: "Ожидаем подключения модели.",
    investigating: "Проверяем связи и уточняем найденные сведения.",
    synthesizing: "Сопоставляем материалы и готовим ответ.", finished: "",
  };
  const ANSWER_STOP_LABELS = {
    model_policy_stop: "Провайдер остановил генерацию",
    model_refusal_text: "Модель не ответила на вопрос",
    answer_quality: "Ответ не прошёл проверку",
  };
  const $ = (id) => document.getElementById(id);
  const layoutState = { historyCollapsed: false, contextCollapsed: false };
  function setPanelCollapsed(panel, collapsed, { persist = true } = {}) {
    const content = $(panel + "-content"), toggle = $(panel + "-toggle");
    if (collapsed && content.contains(document.activeElement)) toggle.focus();
    layoutState[panel + "Collapsed"] = collapsed;
    content.hidden = collapsed;
    $("workspace").classList.toggle(panel + "-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    const label = (collapsed ? "Показать " : "Свернуть ") + (panel === "history" ? "список чатов" : "источники");
    toggle.setAttribute("aria-label", label); toggle.title = label;
    if (panel === "context") $("context-shortcut").hidden = !collapsed;
    if (persist) {
      try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layoutState)); } catch (_) { /* The layout still works without storage. */ }
    }
  }
  function initLayout() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "null"); } catch (_) { /* Use the viewport defaults. */ }
    for (const [panel, query] of [["history", "(max-width: 760px)"], ["context", "(max-width: 1200px)"]]) {
      const key = panel + "Collapsed";
      setPanelCollapsed(panel, typeof saved?.[key] === "boolean" ? saved[key] : window.matchMedia(query).matches, { persist: false });
      $(panel + "-toggle").addEventListener("click", () => setPanelCollapsed(panel, !layoutState[key]));
      $(panel + "-panel").addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !layoutState[key]) { setPanelCollapsed(panel, true); $(panel + "-toggle").focus(); }
      });
    }
    $("context-shortcut").addEventListener("click", () => {
      setPanelCollapsed("context", false); $("context-toggle").focus();
    });
  }
  function scrollChatToLatest() {
    const scroll = $("chat-scroll");
    scroll.scrollTo?.({ top: scroll.scrollHeight, behavior: "auto" });
  }
  const state = {
    status: null, runs: [], current: null, selected: new Set(), sourcesInitialized: false,
    submitting: false, cancelling: false, needsAuth: false, auth: null, generation: 0, identityEpoch: 0,
    pollTimer: null, pollController: null, refreshController: null, pollDelay: 1400,
    evidenceSignature: null, historySignature: null, answerSignature: null, presentedRunId: null,
    modelDetails: null, modelEndpointAvailable: null, modelError: "", modelBusy: null,
    modelPollTimer: null, modelPollController: null, modelActionController: null,
    sandboxProfiles: [], selectedSandboxProfile: "", sandboxProfilesController: null,
    memoryRunSignature: null,
  };
  const settingsState = {
    canManage: false, checked: false, config: null, saved: "", dirty: false, busy: null,
    controller: null, generation: 0, activeTab: "repositories", message: "", probes: new Map(),
  };
  const sshState = { credentials: [], controller: null, busy: false, editingId: null, generation: 0 };
  const memoryState = { available: false, enabled: true, memories: [], nextOffset: null, total: null, loading: false, editing: null, controller: null, busy: false };
  const discoveryState = { current: [], all: [], signature: null, controller: null, adminController: null, busy: new Set() };
  const deletionState = { target: null, controller: null };
  const feedbackState = { controller: null, busy: false, runId: null, message: "" };
  const modelConnectionState = { settings: null, saved: "", controller: null, busy: false, dirty: false };
  const tlsState = { credentials: [], controller: null, busy: false, editingId: null, generation: 0 };
  const conversationState = { id: null, title: "", runs: [], total: 0, offset: 0, truncated: false, pollError: "", loading: false, controller: null,
    list: [], nextOffset: null, listLoading: false, listController: null, signature: null };
  try {
    const saved = JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
    if (saved && ["bearer", "admin"].includes(saved.kind) && typeof saved.value === "string") {
      state.auth = saved;
    }
  } catch (_) { /* Storage can be unavailable in a restricted browser. */ }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");
  const activeAnimations = new Set();
  function animate(node, frames, finish = () => {}) {
    if (motionPreference.matches || typeof node.animate !== "function") { finish(); return null; }
    const animation = node.animate(frames, { duration: 280, easing: "cubic-bezier(.22,1,.36,1)" });
    activeAnimations.add(animation);
    animation.onfinish = () => { activeAnimations.delete(animation); finish(); };
    animation.oncancel = () => activeAnimations.delete(animation);
    return animation;
  }
  motionPreference.addEventListener("change", () => {
    if (motionPreference.matches) for (const animation of [...activeAnimations]) animation.finish();
  });
  function appear(node) {
    animate(node, [{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }]);
  }
  function enhanceDisclosure(details) {
    const summary = details.querySelector("summary");
    const content = details.querySelector(".disclosure-content");
    let targetOpen = details.open;
    let animation = null;
    summary.setAttribute("aria-expanded", String(targetOpen));
    summary.addEventListener("click", (event) => {
      event.preventDefault();
      targetOpen = !targetOpen;
      const height = details.open ? content.getBoundingClientRect().height : 0;
      const opacity = details.open ? getComputedStyle(content).opacity : 0;
      if (animation) animation.cancel();
      if (targetOpen) details.open = true;
      summary.setAttribute("aria-expanded", String(targetOpen));
      animation = animate(content, [
        { height: height + "px", opacity },
        { height: (targetOpen ? content.scrollHeight : 0) + "px", opacity: targetOpen ? 1 : 0 },
      ], () => { details.open = targetOpen; animation = null; });
    });
  }
  function safeUrl(value) {
    if (typeof value !== "string") return null;
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }
  function link(node, value) {
    const url = safeUrl(value);
    if (!url) return false;
    node.href = url;
    node.target = "_blank";
    node.rel = "noopener noreferrer";
    return true;
  }
  function idOf(run) { return run && String(run.id || run.run_id || ""); }
  function dateLabel(value, full = false) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("ru-RU", {
      day: "numeric", month: "short", ...(full ? { hour: "2-digit", minute: "2-digit" } : {}),
    }).format(date);
  }
  function modeLabel(value) { return { quick: "Быстрый ответ", investigate: "Исследование", autonomous: "Автопилот" }[value] || "Запрос"; }
  function effortLabel(value) { return { low: "низкое", medium: "обычное", high: "высокое" }[value] || "не указано"; }
  function showError(message) {
    $("error-banner").textContent = message || "";
    $("error-banner").hidden = !message;
  }
  function connection(label, kind) {
    $("connection-label").textContent = label;
    $("connection-state").className = "connection-state " + (kind || "");
  }
  function openAuth() {
    $("auth-kind").value = state.auth ? state.auth.kind : "bearer";
    if (!$("auth-dialog").open) $("auth-dialog").showModal();
    $("auth-secret").focus();
  }
  function requireAuth() {
    state.needsAuth = true;
    stopPolling();
    stopModelPolling();
    state.modelDetails = null;
    resetSettingsAccess();
    resetPersonalData();
    conversationState.listController?.abort();
    Object.assign(conversationState, { list: [], nextOffset: null, listLoading: false, listController: null });
    state.runs = []; resetWorkspace();
    renderModel();
    connection("Требуется вход", "warning");
    showError("Для входа в Harness войдите в портал или укажите ключ доступа к порталу.");
    openAuth();
    updateSubmit();
  }
  async function api(path, { method = "GET", body, signal, timeoutMs } = {}) {
    const identityEpoch = state.identityEpoch;
    // Credential editors clear their input objects immediately after calling api.
    // Capture the payload before awaiting the administrator session.
    const payload = body !== undefined ? JSON.stringify(body) : undefined;
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (state.auth) headers[state.auth.kind === "admin" ? "X-KB-Admin-Password" : "Authorization"] =
      state.auth.kind === "admin" ? state.auth.value : "Bearer " + state.auth.value;
    const controller = new AbortController(), abort = () => controller.abort();
    let timedOut = false, mutationStarted = false;
    if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
    const modelProbe = path.split("?", 1)[0] === "model/connection/probe";
    const wait = timeoutMs || (modelProbe ? Math.min(3600, Number(body?.timeout_seconds || modelConnectionState.settings?.timeout_seconds) || 60) * 1000 + 5000 : method === "GET" ? 20000 : 45000);
    const timer = window.setTimeout(() => { timedOut = true; abort(); }, wait);
    try {
      if (method !== "GET" && /^(settings|model|credentials)(\/|$)/.test(path)) {
        // Portal cookie admins need CSRF proof even when a reader Bearer token
        // supplies their personal identity. Header-only hosts have no session API.
        const session = await fetch(new URL("../access/api/session", document.baseURI), {
          credentials: "same-origin", cache: "no-store", signal: controller.signal,
          headers: { Accept: "application/json" },
        });
        if (session.ok) {
          const data = await session.json();
          if (typeof data.csrf_token !== "string" || !data.csrf_token) throw new Error("Не удалось подтвердить сессию администратора. Обновите вход в портал.");
          headers["X-CSRF-Token"] = data.csrf_token;
        } else if (![401, 403, 404].includes(session.status)) {
          throw new Error("Не удалось проверить сессию администратора. Изменения не отправлены. Повторите попытку.");
        }
      }
      if (identityEpoch !== state.identityEpoch || controller.signal.aborted) {
        const error = new Error("Запрос отменён."); error.name = "AbortError"; throw error;
      }
      mutationStarted = method !== "GET";
      const response = await fetch(new URL("./api/" + path, document.baseURI), {
        method, headers, credentials: "same-origin", signal: controller.signal,
        ...(payload !== undefined ? { body: payload } : {}),
      });
      if (response.status === 401) {
        if (identityEpoch === state.identityEpoch) requireAuth();
        const error = new Error("Требуется вход в Harness."); error.status = 401; throw error;
      }
      let result;
      try { result = await response.json(); }
      catch (_) { const error = new Error("Не удалось прочитать ответ сервера. Обновите чат."); error.status = response.status; throw error; }
      if (!response.ok) {
        const message = response.status === 403 && ["Harness administrator access required", "Model administrator access required"].includes(result.error)
          ? "Нужны права администратора Harness. Войдите в портал как администратор или укажите администраторский ключ/пароль в окне «Вход в Harness», затем повторите действие."
          : typeof result.error === "string" ? result.error : "Не удалось выполнить запрос.";
        const error = new Error(message); error.status = response.status; throw error;
      }
      return result;
    } catch (error) {
      if (timedOut) {
        const timeout = new Error(modelProbe && mutationStarted ? "Истекло время проверки модели. Проверьте адрес и время ожидания." :
          method === "GET" ? "Сервер не ответил вовремя. Последние данные сохранены; можно обновить чат." : mutationStarted
          ? "Не дождались подтверждения сервера. Действие могло выполниться — обновите чат перед повторной отправкой."
          : "Не удалось подтвердить сессию администратора вовремя. Изменения не отправлены. Повторите попытку.");
        timeout.name = "RequestTimeoutError"; timeout.uncertainMutation = mutationStarted && !modelProbe; throw timeout;
      }
      if (method !== "GET" && error instanceof TypeError) error.uncertainMutation = mutationStarted && !modelProbe;
      throw error;
    } finally { window.clearTimeout(timer); signal?.removeEventListener("abort", abort); }
  }
  function errorMessage(error) {
    return error instanceof TypeError ? "Не удалось связаться с сервером. Проверьте подключение." :
      error.message || "Не удалось выполнить запрос. Попробуйте ещё раз.";
  }
  function settingsError(message = "") {
    $("settings-error").textContent = message;
    $("settings-error").hidden = !message;
  }
  function normalizedSettings(value = {}) {
    const strings = (items) => Array.isArray(items) ? items.filter((item) => typeof item === "string") : [];
    const resource = (item) => ({
      id: String(item.id || ""), name: String(item.name || "").trim(), url: String(item.url || "").trim(),
      enabled: item.enabled !== false, credential_env: String(item.credential_env || "").trim(),
      allowed_owners: strings(item.allowed_owners),
    });
    const sandbox = value.sandbox || {};
    return {
      repositories: (Array.isArray(value.repositories) ? value.repositories : []).map((item) => ({
        ...resource(item), ref: String(item.ref || "HEAD").trim(), ssh_key_id: String(item.ssh_key_id || "").trim(),
      })),
      connections: (Array.isArray(value.connections) ? value.connections : []).map((item) => ({
        ...resource(item), kind: item.kind === "a2a" ? "a2a" : "mcp", allowed_tools: strings(item.allowed_tools),
      })),
      sandbox_profiles: (Array.isArray(value.sandbox_profiles) ? value.sandbox_profiles : []).map((item) => ({
        id: String(item.id || ""), name: String(item.name || "").trim(), image: String(item.image || "").trim(),
        enabled: item.enabled !== false, allowed_owners: strings(item.allowed_owners),
      })),
      sandbox: {
        enabled: sandbox.enabled === true, image: String(sandbox.image || "harness-sandbox:local").trim(),
        cpus: sandbox.cpus ?? 1, memory_mb: sandbox.memory_mb ?? 1024, workspace_mb: sandbox.workspace_mb ?? 512,
        pids_limit: sandbox.pids_limit ?? 128,
        timeout_seconds: sandbox.timeout_seconds ?? 90, max_output_chars: sandbox.max_output_chars ?? 12000,
        max_parallel: sandbox.max_parallel ?? 2,
      },
    };
  }
  function resetSettingsAccess() {
    settingsState.generation += 1;
    if (settingsState.controller) settingsState.controller.abort();
    Object.assign(settingsState, { canManage: false, checked: false, config: null, saved: "", dirty: false,
      busy: null, controller: null, message: "" });
    settingsState.probes.clear();
    clearSshEditor();
    clearTlsEditor();
    modelConnectionState.controller?.abort(); tlsState.controller?.abort();
    Object.assign(modelConnectionState, { settings: null, saved: "", controller: null, busy: false, dirty: false });
    Object.assign(tlsState, { credentials: [], controller: null, busy: false });
    for (const id of ["direct-model-url", "direct-model-name", "direct-model-token"]) $(id).value = "";
    $("model-provider").value = "gigacode"; $("model-profile").value = "auto"; modelConnectionState.metadata = null; modelConnectionState.profileDefaults = null; $("direct-model-timeout").value = "60"; $("model-output-tokens").value = "4096"; fillTlsSelect("");
    for (const id of ["ssh-feedback", "tls-feedback", "model-connection-feedback"]) $(id).textContent = "";
    updateModelConnection();
    if (sshState.controller) sshState.controller.abort();
    Object.assign(sshState, { credentials: [], controller: null, busy: false });
    sshState.generation += 1;
    $("settings-open").hidden = true;
    $("control-open").hidden = true;
    if ($("settings-dialog").open) $("settings-dialog").close();
    for (const id of ["settings-repositories", "settings-connections", "settings-sandbox", "settings-sandbox-profiles", "ssh-list", "tls-list", "settings-discoveries"]) $(id).replaceChildren();
    settingsError();
    updateSettingsChrome();
  }
  function updateSettingsChrome() {
    const busy = Boolean(settingsState.busy);
    $("settings-open").hidden = !settingsState.canManage || state.needsAuth;
    $("control-open").hidden = state.status?.can_manage_control !== true || state.needsAuth;
    $("settings-fields").disabled = busy || !settingsState.canManage;
    $("settings-save").disabled = busy || !settingsState.canManage || !settingsState.dirty;
    $("settings-discard").disabled = busy || !settingsState.dirty;
    $("settings-dialog").setAttribute("aria-busy", String(busy));
    $("settings-save").textContent = settingsState.busy === "saving" ? "Сохраняем…" : "Сохранить";
    $("settings-add-repository").disabled = (settingsState.config?.repositories.length || 0) >= 100;
    $("settings-add-connection").disabled = (settingsState.config?.connections.length || 0) >= 100;
    $("settings-add-sandbox-profile").disabled = (settingsState.config?.sandbox_profiles.length || 0) >= 20;
    $("settings-fields").querySelectorAll("[data-probe]").forEach((button) => {
      button.disabled = busy || settingsState.dirty;
      button.title = settingsState.dirty ? "Сначала сохраните изменения" : "Проверить сохранённые настройки";
    });
    const busyText = { loading: "Загружаем настройки…", saving: "Сохраняем настройки…", probing: "Проверяем подключение…" };
    $("settings-feedback").textContent = busyText[settingsState.busy] || (settingsState.dirty ?
      "Есть изменения. Сохраните их перед проверкой подключений." : settingsState.message || "Настройки сохраняются на сервере.");
    const integrationTab = ["repositories", "connections", "sandbox"].includes(settingsState.activeTab);
    $("settings-save").hidden = !integrationTab;
    $("settings-discard").hidden = !integrationTab;
    if (!integrationTab && !settingsState.busy) $("settings-feedback").textContent = settingsState.dirty ?
      "В других разделах остались несохранённые изменения подключений." : "Изменения в этом разделе сохраняются отдельно.";
  }
  function markSettingsChanged() {
    settingsState.dirty = JSON.stringify(settingsState.config) !== settingsState.saved;
    settingsState.message = "";
    settingsError();
    updateSettingsChrome();
  }
  function selectSettingsTab(name, focus = false) {
    if (!SETTINGS_TABS.includes(name)) return;
    settingsState.activeTab = name;
    for (const tab of document.querySelectorAll("[data-settings-tab]")) {
      const active = tab.dataset.settingsTab === name;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      $("settings-panel-" + tab.dataset.settingsTab).hidden = !active;
      if (active && focus) tab.focus();
    }
    if (name === "ssh" && $("settings-dialog").open) loadSshKeys();
    if (name === "discoveries" && $("settings-dialog").open) loadAdminDiscoveries();
    if (name === "model" && $("settings-dialog").open) loadModelConnection();
    updateSettingsChrome();
  }
  function settingField(label, value, onInput, options = {}) {
    const wrapper = element("label", "settings-field" + (options.wide ? " wide" : ""));
    const caption = element("span", "settings-field-label", label);
    const input = document.createElement("input");
    input.type = options.type || "text";
    input.value = value;
    input.autocomplete = "off";
    input.spellcheck = false;
    if (options.required) input.required = true;
    for (const key of ["placeholder", "min", "max", "step", "pattern", "maxLength"]) {
      if (options[key] !== undefined) input[key] = options[key];
    }
    input.addEventListener("input", () => { onInput(input.value); markSettingsChanged(); });
    wrapper.append(caption, input);
    if (options.help) wrapper.append(element("small", "settings-field-help", options.help));
    return { wrapper, input };
  }
  function settingSwitch(value, onChange, label = "Подключение") {
    const wrapper = element("label", "settings-switch");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.setAttribute("role", "switch");
    input.setAttribute("aria-label", label);
    input.checked = value;
    const text = element("span", "", value ? "Включено" : "Выключено");
    input.addEventListener("change", () => {
      onChange(input.checked);
      text.textContent = input.checked ? "Включено" : "Выключено";
      markSettingsChanged();
    });
    wrapper.append(input, text);
    return wrapper;
  }
  function commaValues(value) { return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))]; }
  function settingProbeArea(kind, entry = null, toolsInput = null) {
    const key = kind + ":" + (entry?.id || "");
    const wrapper = element("div", "settings-probe");
    const actions = element("div", "settings-probe-actions");
    const button = element("button", "secondary-button", kind === "sandbox" ? "Проверить песочницу" : kind === "sandbox_profile" ? "Проверить среду" : "Проверить подключение");
    button.type = "button";
    button.dataset.probe = key;
    button.addEventListener("click", () => probeSettings(kind, entry?.id));
    actions.append(button);
    if (kind === "repository") actions.append(element("p", "settings-probe-note", "Проверим адрес и готовность песочницы. Репозиторий будет загружен при исследовании."));
    if (kind === "sandbox_profile") actions.append(element("p", "settings-probe-note", "Проверим готовность выбранного образа на сервере."));
    wrapper.append(actions);
    const result = settingsState.probes.get(key);
    if (result) {
      const message = element("div", "settings-probe-result");
      message.setAttribute("role", "status");
      message.append(element("strong", "", result.available === true ? "Проверка пройдена" : "Пока недоступно"),
        element("p", "", typeof result.message === "string" ? result.message : "Сервер не сообщил подробности."));
      wrapper.append(message);
      const tools = entry?.kind === "mcp" && Array.isArray(result.tools) ? result.tools : [];
      if (tools.length) {
        const choices = element("fieldset", "settings-tool-choices");
        choices.append(element("legend", "", "Выберите, какие инструменты разрешены агенту"));
        const known = new Set();
        for (const tool of tools) {
          if (!tool || typeof tool.name !== "string" || known.has(tool.name)) continue;
          known.add(tool.name);
          const label = element("label", "settings-tool-choice");
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.checked = entry.allowed_tools.includes(tool.name);
          const text = element("span");
          text.append(element("strong", "", tool.name));
          if (typeof tool.description === "string") text.append(element("small", "", tool.description));
          checkbox.addEventListener("change", () => {
            const selected = new Set(entry.allowed_tools);
            if (checkbox.checked) selected.add(tool.name); else selected.delete(tool.name);
            entry.allowed_tools = [...selected];
            if (toolsInput) toolsInput.value = entry.allowed_tools.join(", ");
            markSettingsChanged();
          });
          label.append(checkbox, text);
          choices.append(label);
        }
        wrapper.append(choices);
      }
    }
    return wrapper;
  }
  function renderSettingResource(entry, kind, index) {
    const repository = kind === "repository";
    const card = element("article", "settings-resource");
    const heading = element("div", "settings-resource-heading");
    const title = element("h4", "", entry.name || (repository ? "Репозиторий " : "Подключение ") + (index + 1));
    const controls = element("div", "settings-resource-controls");
    controls.append(settingSwitch(entry.enabled, (value) => { entry.enabled = value; }, entry.name || "Подключение"));
    const remove = element("button", "text-button", "Удалить");
    remove.type = "button";
    remove.setAttribute("aria-label", "Удалить " + (entry.name || (repository ? "репозиторий" : "подключение")));
    remove.addEventListener("click", () => {
      const collection = repository ? "repositories" : "connections";
      settingsState.config[collection] = settingsState.config[collection].filter((item) => item !== entry);
      settingsState.probes.delete(kind + ":" + entry.id);
      markSettingsChanged();
      renderSettings();
      $(repository ? "settings-add-repository" : "settings-add-connection").focus();
    });
    controls.append(remove);
    heading.append(title, controls);
    const fields = element("div", "settings-grid");
    const name = settingField("Название", entry.name, (value) => {
      entry.name = value;
      title.textContent = value || (repository ? "Репозиторий " : "Подключение ") + (index + 1);
    }, { required: true, maxLength: 160 });
    fields.append(name.wrapper);
    let kindSelect = null;
    if (!repository) {
      const label = element("label", "settings-field");
      kindSelect = document.createElement("select");
      for (const [value, text] of [["mcp", "MCP · инструменты"], ["a2a", "A2A · другой агент"]]) {
        const option = element("option", "", text); option.value = value; kindSelect.append(option);
      }
      kindSelect.value = entry.kind;
      label.append(element("span", "settings-field-label", "Тип подключения"), kindSelect);
      fields.append(label);
    } else fields.append(settingField("Ветка, тег или commit", entry.ref, (value) => { entry.ref = value; }, { required: true, maxLength: 200, placeholder: "HEAD" }).wrapper);
    fields.append(settingField(repository ? "Адрес репозитория · HTTPS или SSH" : "Адрес сервера", entry.url, (value) => { entry.url = value; }, {
      type: repository ? "text" : "url", required: true, maxLength: 2000, wide: true,
      placeholder: repository ? "git@git.example.ru:team/service.git" : "https://service.example.ru/mcp",
      help: repository ? "HTTPS, ssh://git@host/team/repo.git или git@host:team/repo.git. Не вставляйте токены и пароли в адрес." :
        "Адрес без токена, логина, пароля и параметров. Для передачи токена используйте переменную на сервере.",
    }).wrapper);
    if (repository) {
      const key = element("label", "settings-field wide");
      const select = document.createElement("select");
      select.dataset.sshKey = entry.id;
      fillSshSelect(select, entry.ssh_key_id);
      select.addEventListener("change", () => { entry.ssh_key_id = select.value; markSettingsChanged(); });
      key.append(element("span", "settings-field-label", "SSH-ключ для клонирования"), select,
        element("small", "settings-field-help", "Для SSH выберите ключ из раздела «SSH-ключи». Для HTTPS оставьте «Не использовать»."));
      fields.append(key);
    }
    fields.append(settingField("Имя переменной с токеном на сервере", entry.credential_env, (value) => { entry.credential_env = value; }, {
      wide: true, pattern: "[A-Z][A-Z0-9_]{0,127}", maxLength: 128, placeholder: "Например, GIT_ACCESS_TOKEN",
      help: "Только имя переменной. Сам токен вводить сюда не нужно.",
    }).wrapper);
    fields.append(settingField("Кому доступно (ID пользователей; пусто — всем вошедшим)", entry.allowed_owners.join(", "),
      (value) => { entry.allowed_owners = commaValues(value); }, { wide: true, help: "Несколько ID укажите через запятую." }).wrapper);
    let toolsInput = null;
    if (!repository) {
      const tools = settingField("Разрешённые инструменты MCP", entry.allowed_tools.join(", "), (value) => {
        entry.allowed_tools = commaValues(value);
        card.querySelectorAll(".settings-tool-choice input").forEach((checkbox) => {
          checkbox.checked = entry.allowed_tools.includes(checkbox.nextElementSibling.firstElementChild.textContent);
        });
      }, { wide: true, help: "Имена через запятую. Пустой список запрещает все инструменты. После проверки можно выбрать инструменты из списка." });
      toolsInput = tools.input;
      tools.wrapper.hidden = entry.kind !== "mcp";
      fields.append(tools.wrapper);
      kindSelect.addEventListener("change", () => {
        entry.kind = kindSelect.value;
        tools.wrapper.hidden = entry.kind !== "mcp";
        settingsState.probes.delete(kind + ":" + entry.id);
        card.querySelector(".settings-probe").replaceWith(settingProbeArea(kind, entry, toolsInput));
        markSettingsChanged();
      });
    }
    card.append(heading, fields, settingProbeArea(kind, entry, toolsInput));
    return card;
  }
  function renderSettings() {
    if (!settingsState.config || !settingsState.canManage) return;
    const { repositories, connections, sandbox } = settingsState.config;
    $("settings-repositories").replaceChildren(...repositories.map((entry, index) => renderSettingResource(entry, "repository", index)));
    $("settings-connections").replaceChildren(...connections.map((entry, index) => renderSettingResource(entry, "connection", index)));
    $("settings-repositories-empty").hidden = repositories.length > 0;
    $("settings-connections-empty").hidden = connections.length > 0;
    const card = element("div", "settings-resource");
    card.append(settingSwitch(sandbox.enabled, (value) => { sandbox.enabled = value; }, "Использовать песочницы"));
    const fields = element("div", "settings-grid");
    fields.append(settingField("Образ песочницы", sandbox.image, (value) => { sandbox.image = value; }, {
      required: true, wide: true, help: "Образ должен быть подготовлен на сервере. Проверка сообщит, если он отсутствует.",
    }).wrapper);
    for (const [key, label, min, max, step] of [
      ["cpus", "Процессорные ядра", 0.25, 8, "any"], ["memory_mb", "Память, МБ", 128, 16384, 1],
      ["workspace_mb", "Место для файлов, МБ", 64, 8192, 1],
      ["pids_limit", "Максимум процессов", 16, 1024, 1], ["timeout_seconds", "Время выполнения, секунды", 5, 600, 1],
      ["max_output_chars", "Максимальный ответ, символы", 1000, 48000, 1], ["max_parallel", "Параллельные операции", 1, 8, 1],
    ]) fields.append(settingField(label, sandbox[key], (value) => { sandbox[key] = value === "" ? null : Number(value); }, {
      type: "number", required: true, min, max, step,
    }).wrapper);
    card.append(fields, settingProbeArea("sandbox"));
    $("settings-sandbox").replaceChildren(card);
    $("settings-sandbox-profiles").replaceChildren(...settingsState.config.sandbox_profiles.map(renderSandboxProfile));
    $("settings-sandbox-profiles-empty").hidden = settingsState.config.sandbox_profiles.length > 0;
    selectSettingsTab(settingsState.activeTab);
    updateSettingsChrome();
  }
  function renderSandboxProfile(profile, index) {
    const card = element("article", "settings-resource"), heading = element("div", "settings-resource-heading");
    const title = element("h4", "", profile.name || "Среда " + (index + 1));
    const controls = element("div", "settings-resource-controls");
    controls.append(settingSwitch(profile.enabled, (enabled) => { profile.enabled = enabled; }, profile.name || "Среда выполнения"));
    const remove = element("button", "text-button", "Удалить"); remove.type = "button";
    remove.addEventListener("click", () => {
      settingsState.config.sandbox_profiles = settingsState.config.sandbox_profiles.filter((item) => item !== profile);
      markSettingsChanged(); renderSettings(); $("settings-add-sandbox-profile").focus();
    });
    controls.append(remove); heading.append(title, controls);
    const fields = element("div", "settings-grid");
    fields.append(settingField("Название среды", profile.name, (value) => {
      profile.name = value; title.textContent = value || "Среда " + (index + 1);
    }, { required: true, wide: true, maxLength: 160, placeholder: "Например, Java 21" }).wrapper);
    fields.append(settingField("Образ среды на сервере", profile.image, (value) => { profile.image = value; }, {
      required: true, wide: true, maxLength: 251, placeholder: "harness-sandbox-java21:local",
      help: "Образ должен быть подготовлен на сервере. Агент использует выбранную среду и не меняет её образ самостоятельно.",
    }).wrapper);
    fields.append(settingField("Кому доступно (ID пользователей; пусто — всем вошедшим)", profile.allowed_owners.join(", "),
      (value) => { profile.allowed_owners = commaValues(value); }, { wide: true, help: "Несколько ID укажите через запятую." }).wrapper);
    card.append(heading, fields, settingProbeArea("sandbox_profile", profile));
    return card;
  }
  function addSandboxProfile() {
    if (!settingsState.canManage || !settingsState.config || settingsState.busy || settingsState.config.sandbox_profiles.length >= 20) return;
    settingsState.config.sandbox_profiles.push({ id: newResourceId("profile"), name: "", image: "", enabled: false, allowed_owners: [] });
    markSettingsChanged(); renderSettings();
    $("settings-sandbox-profiles").lastElementChild?.querySelector("input[type='text']")?.focus();
  }
  async function loadSettings(force = false) {
    if (state.needsAuth || settingsState.busy || (!force && settingsState.checked) || settingsState.dirty) return;
    const controller = new AbortController(), generation = settingsState.generation, identityEpoch = state.identityEpoch;
    settingsState.controller = controller;
    settingsState.busy = "loading";
    updateSettingsChrome();
    try {
      const response = await api("settings", { signal: controller.signal });
      if (controller.signal.aborted || generation !== settingsState.generation || identityEpoch !== state.identityEpoch) return;
      settingsState.checked = true;
      if (response.can_manage !== true) { resetSettingsAccess(); settingsState.checked = true; return; }
      settingsState.canManage = true;
      settingsState.config = normalizedSettings(response.settings);
      settingsState.saved = JSON.stringify(settingsState.config);
      settingsState.dirty = false;
      settingsState.probes.clear();
      settingsState.message = "Настройки загружены. Изменения применятся после сохранения.";
      settingsError();
      renderSettings();
      loadSshKeys();
      if (window.location?.hash === "#settings") openSettings();
    } catch (error) {
      if (controller.signal.aborted || generation !== settingsState.generation || error.name === "AbortError") return;
      if ([403, 404].includes(error.status)) { resetSettingsAccess(); settingsState.checked = true; }
      else if (error.status !== 401 && $("settings-dialog").open) settingsError(errorMessage(error));
    } finally {
      if (settingsState.controller === controller) { settingsState.controller = null; settingsState.busy = null; updateSettingsChrome(); }
    }
  }
  function openSettings() {
    if (!settingsState.canManage || state.needsAuth) return;
    if (!$("settings-dialog").open) $("settings-dialog").showModal();
    selectSettingsTab(settingsState.activeTab, true);
    loadSettings(true);
  }
  function addSettingResource(kind) {
    if (!settingsState.canManage || settingsState.busy) return;
    const repository = kind === "repository", collection = repository ? "repositories" : "connections";
    if (settingsState.config[collection].length >= 100) return;
    const suffix = typeof globalThis.crypto?.randomUUID === "function" ? crypto.randomUUID().slice(0, 8) :
      Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    const id = (repository ? "repo-" : "connection-") + suffix;
    settingsState.config[collection].push({ id, name: "", url: "", enabled: true, credential_env: "", allowed_owners: [],
      ...(repository ? { ref: "HEAD", ssh_key_id: "" } : { kind: "mcp", allowed_tools: [] }) });
    markSettingsChanged();
    renderSettings();
    const card = $("settings-" + collection).lastElementChild;
    card.querySelector('input[type="text"]').focus();
    appear(card);
  }
  async function saveSettings() {
    if (!settingsState.canManage || !settingsState.dirty || settingsState.busy || state.needsAuth ||
        !["repositories", "connections", "sandbox"].includes(settingsState.activeTab)) return;
    const invalid = ["repositories", "connections", "sandbox"].map((name) => $("settings-panel-" + name).querySelector(":invalid")).find(Boolean);
    if (invalid) {
      const panel = invalid.closest('[role="tabpanel"]');
      if (panel) selectSettingsTab(panel.id.replace("settings-panel-", ""));
      invalid.reportValidity();
      return;
    }
    const controller = new AbortController(), generation = settingsState.generation, identityEpoch = state.identityEpoch;
    settingsState.controller = controller;
    settingsState.busy = "saving";
    settingsError();
    updateSettingsChrome();
    try {
      const response = await api("settings", { method: "PUT", body: normalizedSettings(settingsState.config), signal: controller.signal });
      if (controller.signal.aborted || generation !== settingsState.generation || identityEpoch !== state.identityEpoch) return;
      if (response.can_manage !== true) { resetSettingsAccess(); return; }
      settingsState.config = normalizedSettings(response.settings);
      settingsState.saved = JSON.stringify(settingsState.config);
      settingsState.dirty = false;
      settingsState.probes.clear();
      settingsState.message = "Настройки сохранены. Теперь можно проверить подключения.";
      renderSettings();
      loadAdminDiscoveries();
      loadRunDiscoveries(true);
      loadWorkspace({ refreshModelState: false });
    } catch (error) {
      if (controller.signal.aborted || generation !== settingsState.generation || error.name === "AbortError") return;
      if (error.status === 403) { resetSettingsAccess(); showError("Для изменения настроек нужны права администратора."); }
      else if (error.status !== 401) settingsError(errorMessage(error));
    } finally {
      if (settingsState.controller === controller) { settingsState.controller = null; settingsState.busy = null; updateSettingsChrome(); }
    }
  }
  async function probeSettings(kind, id) {
    if (!settingsState.canManage || settingsState.dirty || settingsState.busy || state.needsAuth) return;
    const controller = new AbortController(), generation = settingsState.generation, identityEpoch = state.identityEpoch;
    settingsState.controller = controller;
    settingsState.busy = "probing";
    settingsError();
    updateSettingsChrome();
    try {
      const result = await api("settings/probe", { method: "POST", body: { kind, ...(id ? { id } : {}) }, signal: controller.signal });
      if (controller.signal.aborted || generation !== settingsState.generation || identityEpoch !== state.identityEpoch) return;
      settingsState.probes.set(kind + ":" + (id || ""), result);
      settingsState.message = result.available === true ? "Проверка завершена." : "Подключение пока недоступно. Подробности — в его настройках.";
      renderSettings();
      const panel = kind === "repository" ? "repositories" : kind === "connection" ? "connections" : "sandbox";
      if ($("settings-dialog").open && settingsState.activeTab === panel) {
        const key = kind + ":" + (id || "");
        const button = [...$("settings-fields").querySelectorAll("[data-probe]")].find((item) => item.dataset.probe === key);
        // The fieldset is enabled in finally; restore focus once that happens.
        queueMicrotask(() => { if (settingsState.generation === generation && $("settings-dialog").open) button?.focus(); });
      }
    } catch (error) {
      if (controller.signal.aborted || generation !== settingsState.generation || error.name === "AbortError") return;
      if (error.status === 403) { resetSettingsAccess(); showError("Для проверки подключений нужны права администратора."); }
      else if (error.status !== 401) settingsError(errorMessage(error));
    } finally {
      if (settingsState.controller === controller) { settingsState.controller = null; settingsState.busy = null; updateSettingsChrome(); }
    }
  }
  function newResourceId(prefix) {
    return prefix + "-" + (typeof globalThis.crypto?.randomUUID === "function" ? crypto.randomUUID().slice(0, 12) :
      Date.now().toString(36) + Math.random().toString(36).slice(2, 7));
  }
  function modelConnectionValue() {
    return { provider: $("model-provider").value, profile: $("model-profile").value || "auto", base_url: $("direct-model-url").value.trim(), model: $("direct-model-name").value.trim(),
      api_key_env: $("direct-model-token").value.trim(), tls_bundle_id: $("direct-model-tls").value, timeout_seconds: Number($("direct-model-timeout").value) || 60,
      output_tokens: Number($("model-output-tokens").value) || 4096 };
  }
  function selectedProfileDefaults() {
    const selected = $("model-profile").value || "auto", metadata = modelConnectionState.metadata || {};
    if (selected === "auto") return $("direct-model-name").value.trim() === (modelConnectionState.settings?.model || "") ? metadata.profile_defaults || null : null;
    return metadata.profiles?.find((profile) => profile.id === selected)?.defaults ||
      (selected === "deepseek_v4_flash" ? { timeout_seconds: 1800, output_tokens: 32768, model_window_tokens: 256000 } : { timeout_seconds: 60, output_tokens: 4096, model_window_tokens: 65536 });
  }
  function changeModelProfile() {
    const previous = modelConnectionState.profileDefaults, next = selectedProfileDefaults();
    if (previous && next) {
      // Carry untouched defaults to the new profile; retain each custom override independently.
      for (const [field, input] of [["timeout_seconds", "direct-model-timeout"], ["output_tokens", "model-output-tokens"]]) {
        if (Number($(input).value) === previous[field]) $(input).value = next[field];
      }
    }
    modelConnectionState.profileDefaults = next;
    updateModelConnection();
  }
  function applyProfileDefaults() {
    const defaults = selectedProfileDefaults();
    if (!defaults || modelConnectionState.busy) return;
    $("direct-model-timeout").value = defaults.timeout_seconds;
    $("model-output-tokens").value = defaults.output_tokens;
    modelConnectionState.profileDefaults = defaults;
    updateModelConnection();
    $("model-connection-feedback").textContent = "Рекомендуемые значения применены. Сохраните подключение, чтобы использовать их в новых запросах.";
  }
  function updateModelConnection() {
    const direct = $("model-provider").value === "openai";
    $("direct-model-fields").hidden = !direct;
    const defaults = selectedProfileDefaults();
    $("model-profile-apply").disabled = modelConnectionState.busy || !defaults;
    $("model-profile-description").textContent = defaults ? "Рекомендация: контекст " + Number(defaults.model_window_tokens).toLocaleString("ru-RU") + " токенов · ожидание " + Math.round(defaults.timeout_seconds / 60) + " мин · ответ до " + Number(defaults.output_tokens).toLocaleString("ru-RU") + " токенов." : "Сохраните подключение, чтобы определить профиль, или выберите его вручную.";
    modelConnectionState.dirty = Boolean(modelConnectionState.saved) && JSON.stringify(modelConnectionValue()) !== modelConnectionState.saved;
    $("model-connection-save").disabled = modelConnectionState.busy || !modelConnectionState.settings || !modelConnectionState.dirty;
    $("model-connection-probe").disabled = modelConnectionState.busy || !modelConnectionState.settings;
    $("model-connection-probe").title = "Отправить простой запрос с текущими настройками, не сохраняя их";
    $("model-autopilot-probe").hidden = !direct;
    $("model-autopilot-probe").disabled = !direct || $("model-connection-probe").disabled;
    $("settings-panel-model").querySelectorAll(".model-connection-fields input, .model-connection-fields select").forEach((input) => { input.disabled = modelConnectionState.busy; });
  }
  function fillTlsSelect(value) {
    const select = $("direct-model-tls");
    const option = element("option", "", "Системные доверенные сертификаты"); option.value = ""; select.replaceChildren(option);
    for (const bundle of tlsState.credentials) { const item = element("option", "", bundle.name || bundle.id); item.value = bundle.id; select.append(item); }
    if (value && !tlsState.credentials.some((item) => item.id === value)) { const item = element("option", "", "Недоступный набор · " + value); item.value = value; select.append(item); }
    select.value = value || "";
  }
  function applyModelConnection(settings, metadata = {}) {
    modelConnectionState.settings = settings;
    modelConnectionState.metadata = metadata;
    $("model-profile").value = settings.profile || "auto";
    $("model-provider").value = settings.provider === "openai" ? "openai" : "gigacode";
    $("direct-model-url").value = settings.base_url || ""; $("direct-model-name").value = settings.model || "";
    $("direct-model-token").value = settings.api_key_env || ""; $("direct-model-timeout").value = settings.timeout_seconds || 60;
    $("model-output-tokens").value = settings.output_tokens || 4096;
    fillTlsSelect(settings.tls_bundle_id);
    modelConnectionState.profileDefaults = selectedProfileDefaults() ||
      (!(settings.profile && settings.profile !== "auto") && !metadata.resolved_profile ? { timeout_seconds: 60, output_tokens: 4096, model_window_tokens: 65536 } : null);
    modelConnectionState.saved = JSON.stringify(modelConnectionValue()); modelConnectionState.dirty = false;
    updateModelConnection();
  }
  async function loadModelConnection() {
    if (!settingsState.canManage || modelConnectionState.busy || modelConnectionState.dirty || state.needsAuth) return;
    modelConnectionState.controller?.abort();
    const controller = new AbortController(), epoch = state.identityEpoch; modelConnectionState.controller = controller;
    modelConnectionState.busy = true; updateModelConnection();
    try {
      const response = await api("model/connection", { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch || !settingsState.canManage) return;
      tlsState.credentials = Array.isArray(response.tls_bundles) ? response.tls_bundles : [];
      applyModelConnection(response.settings || {}, response); renderTlsBundles();
    } catch (error) { if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) $("model-connection-feedback").textContent = errorMessage(error); }
    finally { if (modelConnectionState.controller === controller) { modelConnectionState.controller = null; modelConnectionState.busy = false; updateModelConnection(); } }
  }
  async function changeModelConnection(action = "save") {
    if (modelConnectionState.busy || !settingsState.canManage || state.needsAuth) return;
    const config = modelConnectionValue();
    const autopilot = action === "autopilot", probe = action !== "save";
    if (autopilot && config.provider !== "openai") return;
    if (config.provider === "openai") {
      if (!config.base_url) { $("model-connection-feedback").textContent = "Укажите HTTPS-адрес API модели. Название модели и ключ необязательны."; return; }
      for (const input of $("direct-model-fields").querySelectorAll("input")) { if (!input.checkValidity()) { input.reportValidity(); return; } }
    }
    const controller = new AbortController(), epoch = state.identityEpoch;
    modelConnectionState.controller = controller; modelConnectionState.busy = true; updateModelConnection();
    $("model-connection-feedback").textContent = autopilot ? "Проверяем ответ и выбор инструмента…" :
      probe ? "Отправляем простой запрос модели с текущими настройками…" : "Сохраняем подключение…";
    try {
      const route = probe ? "model/connection/probe" + (autopilot ? "?autopilot=1" : "") : "model/connection";
      const response = await api(route, { method: probe ? "POST" : "PUT", body: config, signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch) return;
      if (probe) $("model-connection-feedback").textContent = (response.message || (response.available ? "Модель ответила на проверочный запрос." : "Подключение пока недоступно.")) +
        (modelConnectionState.dirty ? " Настройки ещё не сохранены." : "");
      else {
        if (Array.isArray(response.tls_bundles)) tlsState.credentials = response.tls_bundles;
        applyModelConnection(response.settings || config, response);
        $("model-connection-feedback").textContent = "Подключение сохранено. Новые запросы будут использовать выбранную модель.";
      }
      if (!probe) {
        state.modelEndpointAvailable = null; state.modelDetails = null; state.modelError = "";
        loadWorkspace();
      }
    } catch (error) { if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) $("model-connection-feedback").textContent = errorMessage(error); }
    finally { if (modelConnectionState.controller === controller) { modelConnectionState.controller = null; modelConnectionState.busy = false; updateModelConnection(); } }
  }
  function clearTlsEditor() {
    tlsState.generation += 1; tlsState.editingId = null;
    for (const id of ["tls-name", "tls-ca", "tls-cert", "tls-key", "tls-ca-file", "tls-cert-file", "tls-key-file"]) $(id).value = "";
    $("tls-editor").hidden = true;
  }
  function openTlsEditor(bundle = null) {
    if (tlsState.busy) return;
    clearTlsEditor(); tlsState.editingId = bundle?.id || null;
    $("tls-name").value = bundle?.name || "";
    $("tls-editor-title").textContent = bundle ? "Заменить сертификаты" : "Набор сертификатов";
    $("tls-editor").hidden = false; $("tls-name").focus();
  }
  function renderTlsBundles() {
    $("tls-list").replaceChildren(...tlsState.credentials.map((bundle) => {
      const card = element("article", "settings-resource"), heading = element("div", "settings-resource-heading");
      heading.append(element("h4", "", bundle.name || bundle.id));
      const actions = element("div", "resource-actions");
      const replace = element("button", "text-button", "Заменить"); replace.type = "button"; replace.addEventListener("click", () => openTlsEditor(bundle));
      const remove = element("button", "text-button", "Удалить"); remove.type = "button"; remove.addEventListener("click", () => openDeletion("tls", bundle));
      remove.disabled = modelConnectionState.settings?.tls_bundle_id === bundle.id || $("direct-model-tls").value === bundle.id;
      if (remove.disabled) remove.title = "Сначала выберите другие сертификаты в подключении модели и сохраните его";
      actions.append(replace, remove); heading.append(actions);
      card.append(heading, element("p", "settings-field-help", bundle.has_client_certificate ? "С клиентским сертификатом · mTLS" : "Сертификаты доверенного центра"));
      if (bundle.expires_at) card.append(element("p", "settings-field-help", "Действует до: " + dateLabel(bundle.expires_at)));
      return card;
    }));
    $("tls-empty").hidden = tlsState.credentials.length > 0;
  }
  async function loadTlsBundles() {
    if (!settingsState.canManage || tlsState.busy || state.needsAuth) return;
    tlsState.controller?.abort();
    const controller = new AbortController(), epoch = state.identityEpoch; tlsState.controller = controller;
    try {
      const response = await api("credentials/tls", { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch || !settingsState.canManage) return;
      const selected = $("direct-model-tls").value;
      tlsState.credentials = Array.isArray(response.credentials) ? response.credentials : [];
      fillTlsSelect(selected); renderTlsBundles();
    } catch (error) { if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) $("tls-feedback").textContent = errorMessage(error); }
    finally { if (tlsState.controller === controller) tlsState.controller = null; }
  }
  async function saveTlsBundle() {
    if (tlsState.busy || !settingsState.canManage || state.needsAuth) return;
    const body = { id: tlsState.editingId || newResourceId("tls"), name: $("tls-name").value.trim(), ca_certificate: $("tls-ca").value.trim(),
      client_certificate: $("tls-cert").value.trim(), client_key: $("tls-key").value.trim() };
    if (!body.name || (!body.ca_certificate && !body.client_certificate) || Boolean(body.client_certificate) !== Boolean(body.client_key)) {
      $("tls-feedback").textContent = "Укажите название и CA-сертификат или клиентский сертификат вместе с его приватным ключом."; return;
    }
    const controller = new AbortController(), epoch = state.identityEpoch; tlsState.controller?.abort(); tlsState.controller = controller; tlsState.busy = true;
    $("tls-save").disabled = true; $("tls-new").disabled = true; $("tls-feedback").textContent = "Сохраняем сертификаты…";
    const request = api("credentials/tls", { method: "POST", body, signal: controller.signal });
    body.client_key = ""; body.client_certificate = ""; body.ca_certificate = ""; clearTlsEditor();
    try {
      await request;
      if (controller.signal.aborted || epoch !== state.identityEpoch) return;
      $("tls-feedback").textContent = "Сертификаты сохранены. Выберите их в подключении модели.";
    } catch (error) { if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) $("tls-feedback").textContent = errorMessage(error) + " Поля сертификатов очищены."; }
    finally { if (tlsState.controller === controller) { tlsState.controller = null; tlsState.busy = false; $("tls-save").disabled = false; $("tls-new").disabled = false; if (epoch === state.identityEpoch && !controller.signal.aborted) loadTlsBundles(); } }
  }
  function fillSshSelect(select, value) {
    const empty = element("option", "", "Не использовать"); empty.value = "";
    select.replaceChildren(empty);
    for (const key of sshState.credentials) {
      const option = element("option", "", key.name || key.id); option.value = key.id; select.append(option);
    }
    if (value && !sshState.credentials.some((key) => key.id === value)) {
      const option = element("option", "", "Недоступный ключ · " + value); option.value = value; select.append(option);
    }
    select.value = value || "";
  }
  function clearSshEditor() {
    sshState.generation += 1;
    sshState.editingId = null;
    for (const id of ["ssh-name", "ssh-file", "ssh-private-key", "ssh-known-hosts"]) $(id).value = "";
    $("ssh-editor").hidden = true;
  }
  function openSshEditor(key = null) {
    if (sshState.busy) return;
    clearSshEditor();
    sshState.editingId = key?.id || null;
    $("ssh-name").value = key?.name || "";
    $("ssh-editor-title").textContent = key ? "Заменить SSH-ключ" : "Новый SSH-ключ";
    $("ssh-editor").hidden = false;
    $("ssh-name").focus();
  }
  function renderSshKeys() {
    $("ssh-list").replaceChildren(...sshState.credentials.map((key) => {
      const card = element("article", "settings-resource");
      const heading = element("div", "settings-resource-heading");
      heading.append(element("h4", "", key.name || key.id));
      const actions = element("div", "resource-actions");
      const replace = element("button", "text-button", "Заменить ключ"); replace.type = "button";
      replace.addEventListener("click", () => openSshEditor(key));
      const remove = element("button", "text-button", "Удалить"); remove.type = "button";
      remove.addEventListener("click", () => openDeletion("ssh", key));
      const referenced = settingsState.config?.repositories.some((repo) => repo.ssh_key_id === key.id);
      remove.disabled = Boolean(referenced);
      if (referenced) remove.title = "Сначала удалите ключ из настроек репозиториев и сохраните изменения";
      actions.append(replace, remove); heading.append(actions);
      card.append(heading, element("p", "credential-fingerprint", key.fingerprint || ""),
        element("p", "settings-field-help", [key.key_type, key.host_count ? "Проверенных серверов: " + key.host_count : "", dateLabel(key.created_at)].filter(Boolean).join(" · ")));
      return card;
    }));
    $("ssh-empty").hidden = sshState.credentials.length > 0;
    for (const select of document.querySelectorAll("select[data-ssh-key]")) {
      const entry = settingsState.config?.repositories.find((repo) => repo.id === select.dataset.sshKey);
      if (entry) fillSshSelect(select, entry.ssh_key_id);
    }
  }
  async function loadSshKeys() {
    if (!settingsState.canManage || sshState.busy || state.needsAuth) return;
    const controller = new AbortController(), epoch = state.identityEpoch;
    sshState.controller?.abort(); sshState.controller = controller;
    try {
      const result = await api("credentials/ssh", { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch || !settingsState.canManage) return;
      sshState.credentials = Array.isArray(result.credentials) ? result.credentials : [];
      renderSshKeys();
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) $("ssh-feedback").textContent = errorMessage(error);
    } finally { if (sshState.controller === controller) sshState.controller = null; }
  }
  async function saveSshKey() {
    if (sshState.busy || !settingsState.canManage || state.needsAuth) return;
    const name = $("ssh-name").value.trim(), knownHosts = $("ssh-known-hosts").value.trim();
    let privateKey = $("ssh-private-key").value.trim();
    if (!name || !privateKey || !knownHosts) { $("ssh-feedback").textContent = "Укажите название, приватный ключ и проверенные записи known_hosts."; return; }
    const controller = new AbortController(), epoch = state.identityEpoch;
    sshState.controller?.abort(); sshState.controller = controller; sshState.busy = true;
    $("ssh-save").disabled = true; $("ssh-new").disabled = true;
    $("ssh-feedback").textContent = "Сохраняем ключ…";
    const request = api("credentials/ssh", { method: "POST", signal: controller.signal,
      body: { id: sshState.editingId || newResourceId("ssh"), name, private_key: privateKey, known_hosts: knownHosts } });
    privateKey = "";
    clearSshEditor();
    try {
      await request;
      if (controller.signal.aborted || epoch !== state.identityEpoch) return;
      $("ssh-feedback").textContent = "Ключ сохранён. Теперь выберите его у нужного репозитория.";
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) $("ssh-feedback").textContent = errorMessage(error) + " Поля ключа очищены; при повторе загрузите его снова.";
    } finally {
      if (sshState.controller === controller) {
        sshState.controller = null; sshState.busy = false;
        $("ssh-save").disabled = false; $("ssh-new").disabled = false;
        if (!controller.signal.aborted && epoch === state.identityEpoch) loadSshKeys();
      }
    }
  }
  function discoveryCard(item, admin = false) {
    const card = element("article", admin ? "settings-resource" : "discovery-item");
    const labels = { repository: "Репозиторий", mcp: "MCP", a2a: "A2A", web: "Ссылка" };
    const statuses = { discovered: "Обнаружено", requested: "Доступ запрошен", configured: "Настроено", dismissed: "Скрыто" };
    card.append(element("p", "discovery-meta", (labels[item.kind] || "Ресурс") + " · " + (statuses[item.status] || "Обнаружено")),
      element(admin ? "h4" : "h3", "discovery-name", item.name || item.url));
    const url = element(safeUrl(item.url) ? "a" : "p", "discovery-url", item.url);
    if (url.tagName === "A") link(url, item.url);
    card.append(url);
    if (admin && item.owner_id) card.append(element("p", "settings-field-help", "Запрос от: " + item.owner_id));
    if (item.kind === "web") card.append(element("p", "settings-field-help", "Обычная ссылка. Чтобы использовать сервис как инструмент, нужен его адрес MCP или A2A."));
    const actions = element("div", "resource-actions");
    if (item.status !== "configured" && item.status !== "dismissed") {
      if (admin && item.kind !== "web") {
        const configure = element("button", "secondary-button", "Настроить подключение"); configure.type = "button";
        configure.addEventListener("click", () => configureDiscovery(item)); actions.append(configure);
      } else if (item.status !== "requested") {
        const request = element("button", "text-button", "Запросить доступ"); request.type = "button";
        request.disabled = discoveryState.busy.has(item.id);
        request.addEventListener("click", () => changeDiscovery(item, "request")); actions.append(request);
      }
      const dismiss = element("button", "text-button", "Скрыть"); dismiss.type = "button";
      dismiss.disabled = discoveryState.busy.has(item.id);
      dismiss.addEventListener("click", () => changeDiscovery(item, "dismiss")); actions.append(dismiss);
    }
    if (item.status === "configured") card.append(element("p", "settings-field-help", "Подключение сохранено в настройках. Для исследования его нужно включить и выбрать в области поиска."));
    card.append(actions);
    return card;
  }
  function renderDiscoveries() {
    const items = discoveryState.current.filter((item) => item.status !== "dismissed");
    $("run-discoveries").replaceChildren(...items.map((item) => discoveryCard(item, settingsState.canManage)));
    $("discoveries-count").textContent = String(items.length);
    $("discoveries-card").hidden = !state.current || !items.length;
    const all = discoveryState.all.filter((item) => item.status !== "dismissed");
    $("settings-discoveries").replaceChildren(...all.map((item) => discoveryCard(item, true)));
    $("settings-discoveries-empty").hidden = all.length > 0;
  }
  async function loadRunDiscoveries(force = false) {
    if (!state.current || state.needsAuth) return;
    const runId = idOf(state.current), epoch = state.identityEpoch;
    const signature = JSON.stringify([runId, state.current.status, state.current.phase, state.current.evidence?.length]);
    if (!force && discoveryState.signature === signature) return;
    discoveryState.signature = signature;
    discoveryState.controller?.abort();
    const controller = new AbortController(); discoveryState.controller = controller;
    try {
      const result = await api("discoveries?run_id=" + encodeURIComponent(runId), { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch || runId !== idOf(state.current)) return;
      discoveryState.current = Array.isArray(result.discoveries) ? result.discoveries : [];
      renderDiscoveries();
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) {
        discoveryState.signature = null;
        $("discovery-feedback").textContent = error.status === 404 ? "" : errorMessage(error);
      }
    } finally { if (discoveryState.controller === controller) discoveryState.controller = null; }
  }
  async function loadAdminDiscoveries() {
    if (!settingsState.canManage || state.needsAuth) return;
    discoveryState.adminController?.abort();
    const controller = new AbortController(), epoch = state.identityEpoch;
    discoveryState.adminController = controller;
    try {
      const result = await api("discoveries?scope=all", { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch || !settingsState.canManage) return;
      discoveryState.all = Array.isArray(result.discoveries) ? result.discoveries : [];
      renderDiscoveries();
    } catch (error) {
      if (error.name !== "AbortError" && ![401, 404].includes(error.status) && epoch === state.identityEpoch && settingsState.activeTab === "discoveries") settingsError(errorMessage(error));
    } finally { if (discoveryState.adminController === controller) discoveryState.adminController = null; }
  }
  async function changeDiscovery(item, action) {
    if (discoveryState.busy.has(item.id) || state.needsAuth) return;
    const epoch = state.identityEpoch;
    discoveryState.busy.add(item.id); renderDiscoveries();
    try {
      await api("discoveries/" + encodeURIComponent(item.id) + "/" + action, { method: "POST", body: {} });
      if (epoch !== state.identityEpoch) return;
      $("discovery-feedback").textContent = action === "request" ? "Запрос появился у администратора в разделе «Обнаруженные»." : "Ресурс скрыт.";
      await Promise.all([loadRunDiscoveries(true), loadAdminDiscoveries()]);
    } catch (error) {
      if (error.status !== 401 && epoch === state.identityEpoch) { $("discovery-feedback").textContent = errorMessage(error); if ($("settings-dialog").open) settingsError(errorMessage(error)); }
    } finally { if (epoch === state.identityEpoch) { discoveryState.busy.delete(item.id); renderDiscoveries(); } }
  }
  function configureDiscovery(item) {
    if (!settingsState.canManage || !settingsState.config || settingsState.busy) return;
    const repository = item.kind === "repository", collection = repository ? "repositories" : "connections";
    if (!repository && !["mcp", "a2a"].includes(item.kind)) return;
    if (settingsState.config[collection].length >= 100) { settingsError("Достигнут лимит подключений."); return; }
    const duplicate = settingsState.config[collection].find((entry) => entry.url === item.url);
    if (!duplicate) {
      settingsState.config[collection].push({ id: newResourceId(repository ? "repo" : "connection"), name: (item.name || item.url).slice(0, 160),
        url: item.url, enabled: false, credential_env: "", allowed_owners: item.owner_id ? [item.owner_id] : [],
        ...(repository ? { ref: "HEAD", ssh_key_id: "" } : { kind: item.kind, allowed_tools: [] }) });
      markSettingsChanged();
    }
    if (!$("settings-dialog").open) $("settings-dialog").showModal();
    settingsState.activeTab = collection; renderSettings();
    settingsState.message = "Проверьте адрес, настройте доступ и включите подключение. Затем сохраните изменения.";
    updateSettingsChrome();
    const entries = [...$("settings-" + collection).children];
    const index = duplicate ? settingsState.config[collection].indexOf(duplicate) : entries.length - 1;
    entries[index]?.querySelector("input")?.focus();
  }
  function memoryError(message = "") {
    $("memory-error").textContent = message; $("memory-error").hidden = !message;
  }
  function clearMemoryEditor() {
    memoryState.editing = null;
    $("memory-editor").reset(); $("memory-editor").hidden = true;
    $("memory-sources").replaceChildren();
  }
  function renderMemory() {
    $("memory-open").hidden = !memoryState.available || state.needsAuth;
    $("memory-enabled").checked = memoryState.enabled;
    $("memory-enabled").disabled = memoryState.busy;
    $("memory-new").disabled = memoryState.busy;
    const automaticCount = memoryState.memories.filter((record) => record.status === "experience").length;
    const confirmedCount = memoryState.memories.filter((record) => record.status === "active").length;
    const draftCount = memoryState.memories.filter((record) => record.visibility !== "shared").length;
    $("memory-summary").textContent = (memoryState.total === null ? "В списке: " : "Доступно вам: " + memoryState.total + " · Загружено: ") +
      memoryState.memories.length + ". В списке: автоматический опыт — " + automaticCount +
      ", подтверждённые записи — " + confirmedCount + ", ваши черновики — " + draftCount + ".";
    $("memory-load-more").hidden = memoryState.nextOffset === null || !memoryState.available || state.needsAuth;
    $("memory-load-more").disabled = memoryState.busy || memoryState.loading;
    $("memory-load-more").textContent = memoryState.loading ? "Загружаем записи…" : "Показать ещё";
    $("memory-list").replaceChildren(...memoryState.memories.map((record) => {
      const card = element("article", "settings-resource");
      const heading = element("div", "settings-resource-heading");
      heading.append(element("h4", "", record.title));
      const actions = element("div", "resource-actions");
      if (record.can_edit === true) {
        const edit = element("button", "text-button", record.status !== "active" ? "Проверить и подтвердить" : "Изменить"); edit.type = "button";
        edit.addEventListener("click", () => editMemory(record)); edit.disabled = memoryState.busy; actions.append(edit);
      }
      if (record.can_delete === true) {
        const remove = element("button", "text-button", "Удалить"); remove.type = "button";
        remove.addEventListener("click", () => openDeletion("memory", record)); remove.disabled = memoryState.busy; actions.append(remove);
      }
      if (actions.childElementCount) heading.append(actions);
      const kind = { note: "Заметка", correction: "Уточнение", lesson: "Урок из ошибки", case: "Опыт решения" }[record.kind] || "Заметка";
      const visibility = record.visibility === "shared" ? "Общая память АС" : "Ваш личный черновик";
      const status = record.status === "active" ? "Подтверждено автором" : record.status === "experience" ? "Автоматически · требует повторной проверки" : "Не используется в ответах";
      card.append(heading, element("p", "discovery-meta", visibility + " · " + kind + " · " + status),
        element("p", "memory-record-text", record.text), element("p", "settings-field-help", "Источники: " + (record.index_ids || []).map(sourceName).join(", ")));
      const details = [];
      if (record.outcome) details.push("Исход запроса: " + (STATUS_LABELS[record.outcome] || record.outcome));
      if (record.feedback?.rating) details.push("Оценка автора запроса: " + (record.feedback.rating === "helpful" ? "помогло" : "не помогло"));
      details.push("Использовано всеми пользователями: " + Math.max(0, Number(record.usage_count) || 0));
      if (record.quality && typeof record.quality === "object") {
        details.push("Помогло: " + Math.max(0, Number(record.quality.helpful) || 0));
        details.push("Не помогло: " + Math.max(0, Number(record.quality.unhelpful) || 0));
      }
      const learning = { unreviewed: "Повторное использование ещё не оценено", observed: "Опыт использован, оценок пока нет", helpful: "Есть положительные оценки", needs_review: "Есть отрицательные оценки: нужна повторная проверка", mixed: "Оценки различаются" };
      if (learning[record.learning_state]) details.push(learning[record.learning_state]);
      if (record.last_used_at) details.push("Последний раз: " + dateLabel(record.last_used_at, true));
      card.append(element("p", "memory-usage", details.join(" · ")));
      return card;
    }));
    $("memory-empty").hidden = memoryState.memories.length > 0;
    $("remember-run").hidden = !memoryState.available || !state.current || ACTIVE.has(state.current.status);
  }
  async function loadMemory(append = false) {
    if (state.needsAuth || memoryState.busy) return;
    if (append && (memoryState.loading || memoryState.nextOffset === null)) return;
    const offset = append ? memoryState.nextOffset : 0;
    memoryState.controller?.abort();
    const controller = new AbortController(), epoch = state.identityEpoch; memoryState.controller = controller;
    memoryState.loading = true; renderMemory(); memoryError();
    try {
      const result = await api("memory" + (append ? "?offset=" + encodeURIComponent(offset) : ""), { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch || memoryState.controller !== controller) return;
      memoryState.available = true; memoryState.enabled = result.enabled !== false;
      const page = Array.isArray(result.memories) ? result.memories.filter((record) => record && typeof record.id === "string") : [];
      memoryState.memories = [...new Map([...(append ? memoryState.memories : []), ...page].map((record) => [record.id, record])).values()];
      memoryState.nextOffset = Number.isInteger(result.pagination?.next_offset) && result.pagination.next_offset > offset ? result.pagination.next_offset : null;
      memoryState.total = Number.isInteger(result.pagination?.total) && result.pagination.total >= 0 ? result.pagination.total : null;
      const editedRecord = memoryState.editing?.id ? page.find((record) => record.id === memoryState.editing.id) : null;
      if (editedRecord?.can_edit === false) clearMemoryEditor();
      renderMemory();
    } catch (error) {
      if (!controller.signal.aborted && memoryState.controller === controller && error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) {
        if (error.status === 404) { memoryState.available = false; renderMemory(); }
        else if ($("memory-dialog").open) memoryError(errorMessage(error));
      }
    } finally { if (memoryState.controller === controller) { memoryState.controller = null; memoryState.loading = false; renderMemory(); } }
  }
  function openMemory() {
    if (!memoryState.available || state.needsAuth) return;
    if (!$("memory-dialog").open) $("memory-dialog").showModal();
    loadMemory();
  }
  function editMemory(record = null, run = null) {
    if (memoryState.busy) return;
    if (record && record.can_edit !== true) { memoryError("Изменить запись может только её автор."); return; }
    if (run && !Array.isArray(run.index_ids)) { memoryError("Запрос ещё загружается. Дождитесь его открытия и повторите сохранение уточнения."); return; }
    clearMemoryEditor(); memoryError();
    memoryState.editing = { id: record?.id || null, source_run_id: record?.source_run_id || (run ? idOf(run) : null) };
    $("memory-editor-title").textContent = record ? "Изменить запись" : run ? "Запомнить уточнение к ответу" : "Новая запись";
    $("memory-name").value = record?.title || (run?.question || "").slice(0, 160);
    $("memory-text").value = record?.text || "";
    $("memory-kind").value = record?.kind || (run ? "correction" : "note");
    $("memory-confirmed").checked = record?.status === "active";
    const selected = new Set(record?.index_ids || run?.index_ids || [...state.selected]);
    const available = memoryState.editing.source_run_id ? [...selected].map((id) =>
      indexes().find((source) => source.id === id) || { id, name: id }) : indexes();
    for (const source of available) {
      const label = element("label", "memory-source-option");
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.value = source.id;
      checkbox.checked = selected.has(source.id); checkbox.disabled = Boolean(memoryState.editing.source_run_id);
      label.append(checkbox, element("span", "", source.name || source.id)); $("memory-sources").append(label);
    }
    if (memoryState.editing.source_run_id) $("memory-sources").append(element("p", "settings-field-help", "Сохраняется область поиска исходного запроса."));
    memoryState.editing.index_ids = [...selected];
    $("memory-editor").hidden = false;
    $("memory-name").focus();
  }
  async function saveMemory(event) {
    event.preventDefault();
    if (memoryState.busy || !memoryState.editing || state.needsAuth) return;
    const editing = memoryState.editing, epoch = state.identityEpoch;
    const sourceIds = editing.source_run_id ? editing.index_ids : [...$("memory-sources").querySelectorAll("input:checked")].map((input) => input.value);
    if (!sourceIds.length) { memoryError("Выберите хотя бы один связанный источник."); return; }
    const controller = new AbortController(); memoryState.controller?.abort(); memoryState.controller = controller;
    memoryState.loading = false; memoryState.busy = true; $("memory-save").disabled = true; renderMemory(); memoryError();
    const body = { title: $("memory-name").value.trim(), text: $("memory-text").value.trim(), kind: $("memory-kind").value,
      index_ids: sourceIds, confirmed: $("memory-confirmed").checked, ...(editing.source_run_id ? { source_run_id: editing.source_run_id } : {}) };
    try {
      await api("memory" + (editing.id ? "/" + encodeURIComponent(editing.id) : ""), { method: editing.id ? "PUT" : "POST", body, signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch) return;
      clearMemoryEditor();
      $("memory-feedback").textContent = body.confirmed ? "Запись добавлена в общую память АС. Она доступна пользователям с доступом к связанным источникам." : "Ваш личный черновик сохранён. Другие пользователи его не видят, в ответах он не используется.";
    } catch (error) { if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) memoryError(errorMessage(error)); }
    finally {
      if (memoryState.controller === controller) { memoryState.controller = null; memoryState.busy = false; $("memory-save").disabled = false; renderMemory(); if (epoch === state.identityEpoch && !controller.signal.aborted) loadMemory(); }
    }
  }
  async function changeMemoryPreference() {
    if (memoryState.busy || state.needsAuth) return;
    const enabled = $("memory-enabled").checked, epoch = state.identityEpoch;
    memoryState.controller?.abort(); memoryState.controller = null; memoryState.loading = false;
    memoryState.busy = true; renderMemory(); memoryError();
    try {
      const result = await api("memory/preferences", { method: "PUT", body: { enabled } });
      if (epoch !== state.identityEpoch) return;
      memoryState.enabled = result.enabled === true;
      $("memory-feedback").textContent = memoryState.enabled ? "Ваши новые запросы будут использовать общую память АС и автоматически пополнять её опытом." : "Ваши новые запросы не будут использовать и автоматически пополнять память. Существующие общие записи сохранены и доступны другим пользователям.";
    } catch (error) { if (error.status !== 401 && epoch === state.identityEpoch) memoryError(errorMessage(error)); }
    finally { if (epoch === state.identityEpoch) { memoryState.busy = false; renderMemory(); } }
  }
  function openDeletion(kind, item) {
    if (deletionState.controller || state.needsAuth) return;
    if (kind === "memory" && item.can_delete !== true) { memoryError("Удалить запись может только её автор."); return; }
    const active = (kind === "run" || kind === "conversation") && ACTIVE.has(item.status);
    const sharedMemory = kind === "memory" && item.visibility === "shared";
    deletionState.target = { kind, item, active };
    $("delete-title").textContent = kind === "conversation" ? active ? "Остановить исследование в чате?" : "Удалить весь чат?" : kind === "run" ? active ? "Сначала остановить запрос?" : "Удалить запрос?" : kind === "memory" ? sharedMemory ? "Удалить общую запись памяти?" : "Удалить личный черновик?" : kind === "tls" ? "Удалить сертификаты?" : "Удалить SSH-ключ?";
    $("delete-description").textContent = kind === "conversation" ? active ? "В этом чате ещё идёт исследование. Остановите его, затем удалите чат." : "Вся переписка, ответы, материалы и личные черновики этого чата будут удалены без восстановления. Общий опыт АС сохранится. Свои записи общей памяти можно удалить отдельно." : kind === "run" ? active ? "Исследование ещё выполняется. Остановите его, затем удалите запрос из истории." :
      "Этот личный запрос, его продолжения, ответы, найденные материалы и связанные личные черновики будут удалены без восстановления. Общие записи памяти АС сохранятся; удалить свои общие записи можно отдельно в памяти." : kind === "memory" ? sharedMemory ?
      "Запись будет удалена из общей памяти АС и перестанет учитываться в новых ответах для всех пользователей. Ранее сохранённые ответы и личные чаты останутся в истории." :
      "Ваш личный черновик будет удалён без восстановления. Общая память АС и чаты сохранятся." : kind === "tls" ?
      "Сертификаты и связанный приватный ключ будут удалены с сервера. Сначала уберите этот набор из подключения модели." :
      "Ключ будет удалён с сервера. Сначала его нужно убрать из настроек репозиториев.";
    $("delete-confirm").textContent = active ? "Остановить запрос" : "Удалить";
    $("delete-error").hidden = true; $("delete-confirm").disabled = false;
    $("delete-dialog").showModal();
  }
  async function confirmDeletion(event) {
    event.preventDefault();
    const target = deletionState.target;
    if (!target || deletionState.controller || state.needsAuth) return;
    if (target.kind === "memory" && target.item.can_delete !== true) return;
    const epoch = state.identityEpoch, controller = new AbortController(); deletionState.controller = controller;
    $("delete-confirm").disabled = true;
    const id = target.kind === "run" ? idOf(target.item) : target.item.id;
    const path = target.kind === "conversation" ? target.active ? "runs/" + encodeURIComponent(target.item.last_run_id) + "/cancel" : "conversations/" + encodeURIComponent(id) : target.kind === "run" ? "runs/" + encodeURIComponent(id) + (target.active ? "/cancel" : "") :
      target.kind === "memory" ? "memory/" + encodeURIComponent(id) : "credentials/" + (target.kind === "tls" ? "tls" : "ssh") + "/" + encodeURIComponent(id);
    try {
      const result = await api(path, { method: target.active ? "POST" : "DELETE", signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch) return;
      $("delete-dialog").close();
      if (target.kind === "conversation") {
        if (target.active) { rememberRun(result); if (idOf(state.current) === idOf(result)) { state.current = result; renderRun(); } schedulePoll(); }
        else {
          const deletedIds = new Set(Array.isArray(result.deleted_ids) ? result.deleted_ids : []);
          state.runs = state.runs.filter((run) => !deletedIds.has(idOf(run)));
          conversationState.list = conversationState.list.filter((item) => item.id !== id);
          if (conversationState.id === id) resetWorkspace();
          renderHistory(); loadMemory();
        }
        loadConversations();
      } else if (target.kind === "run") {
        if (target.active) { rememberRun(result); if (idOf(state.current) === id) { state.current = result; renderRun(); schedulePoll(); } }
        else {
          const deletedIds = new Set([id, ...(Array.isArray(result.deleted_ids) ? result.deleted_ids.filter((value) => typeof value === "string") : [])]);
          state.refreshController?.abort();
          state.runs = state.runs.filter((run) => !deletedIds.has(idOf(run)));
          if (conversationState.runs.some((run) => deletedIds.has(idOf(run)))) {
            const remaining = conversationState.runs.filter((run) => !deletedIds.has(idOf(run)));
            if (remaining.length) loadConversation({ id: conversationState.id, title: conversationState.title }); else resetWorkspace();
          }
          if (deletedIds.has(memoryState.editing?.source_run_id)) clearMemoryEditor();
          renderHistory(); loadMemory(); loadConversations();
        }
      } else if (target.kind === "memory") { if (memoryState.editing?.id === id) clearMemoryEditor(); loadMemory(); }
      else if (target.kind === "tls") loadTlsBundles();
      else loadSshKeys();
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) { $("delete-error").textContent = errorMessage(error); $("delete-error").hidden = false; }
    } finally { if (deletionState.controller === controller) { deletionState.controller = null; $("delete-confirm").disabled = false; } }
  }
  function resetPersonalData() {
    resetRunFeedback();
    state.memoryRunSignature = null;
    for (const controller of [memoryState.controller, discoveryState.controller, discoveryState.adminController, deletionState.controller]) controller?.abort();
    Object.assign(memoryState, { available: false, enabled: true, memories: [], nextOffset: null, total: null, loading: false, editing: null, controller: null, busy: false });
    Object.assign(discoveryState, { current: [], all: [], signature: null, controller: null, adminController: null, busy: new Set() });
    Object.assign(deletionState, { target: null, controller: null });
    if ($("memory-dialog").open) $("memory-dialog").close();
    if ($("delete-dialog").open) $("delete-dialog").close();
    clearMemoryEditor(); memoryError(); $("memory-feedback").textContent = ""; $("discovery-feedback").textContent = "";
    renderMemory(); renderDiscoveries();
    state.sandboxProfilesController?.abort(); state.sandboxProfilesController = null;
    state.sandboxProfiles = []; state.selectedSandboxProfile = ""; renderExecutionEnvironment();
  }
  function sandboxProfileName(id) {
    return state.sandboxProfiles.find((profile) => profile.id === id)?.name || (id ? id : "По умолчанию");
  }
  function renderExecutionEnvironment() {
    const select = $("sandbox-profile"), selected = state.selectedSandboxProfile;
    const option = element("option", "", "По умолчанию"); option.value = ""; select.replaceChildren(option);
    for (const profile of state.sandboxProfiles) {
      if (!profile.id) continue;
      const item = element("option", "", profile.name || profile.id); item.value = profile.id; select.append(item);
    }
    const unavailable = selected && !state.sandboxProfiles.some((profile) => profile.id === selected);
    if (unavailable) { const item = element("option", "", "Недоступная среда · " + selected); item.value = selected; item.disabled = true; select.append(item); }
    select.value = selected;
    const quick = document.querySelector('input[name="mode"]:checked').value === "quick";
    select.disabled = quick || state.needsAuth || Boolean(state.current && !Array.isArray(state.current.index_ids));
    $("environment-description").textContent = quick ? "Песочница используется в исследовании." : unavailable ?
      "Выбранная среда сейчас недоступна. Выберите другую среду." : state.sandboxProfiles.length ?
      "Подготовленная среда для запуска кода." : "Среды включаются в настройках песочниц.";
  }
  async function loadSandboxProfiles() {
    if (state.needsAuth) return;
    state.sandboxProfilesController?.abort();
    const controller = new AbortController(), epoch = state.identityEpoch; state.sandboxProfilesController = controller;
    try {
      const result = await api("sandbox/profiles", { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch) return;
      state.sandboxProfiles = Array.isArray(result.profiles) ? result.profiles.filter((profile) => profile && typeof profile.id === "string") : [];
      renderExecutionEnvironment();
      if (state.current) renderRunEnvironment(state.current);
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch) {
        state.sandboxProfiles = []; renderExecutionEnvironment();
      }
    } finally { if (state.sandboxProfilesController === controller) state.sandboxProfilesController = null; }
  }
  function renderRunEnvironment(run) {
    $("run-environment").hidden = run.mode === "quick";
    $("run-environment").textContent = "Среда выполнения: " + sandboxProfileName(run.sandbox_profile_id || "") +
      (run.resumable ? ". Продолжение использует исходную среду. Для смены среды начните новый запрос." : ".");
  }
  function indexes() { return Array.isArray(state.status?.indexes) ? state.status.indexes : []; }
  function renderEffortDescription() {
    const mode = document.querySelector('input[name="mode"]:checked')?.value || "quick";
    const policy = state.status?.limits?.effort_policy?.[mode]?.[$("effort").value];
    $("mode-description").textContent = mode !== "quick" ?
      "Базы знаний, код, MCP и другие агенты." : "Базы знаний, без запуска песочниц.";
    const parts = [mode !== "quick" ? "Сначала обходим все выбранные базы знаний." : "Все выбранные базы знаний и один итоговый ответ."];
    if (mode === "autonomous") parts.push("Агент сам запрашивает дополнительные ресурсы в пределах потолков администратора. Все расширения видны в чате.");
    if (policy && mode !== "quick") {
      parts.push("Чтение документов: до " + policy.document_reads + ". Уточняющих обращений: до " + policy.source_calls + ".");
    } else if (mode === "quick") parts.push("Усилие определяет объём найденных материалов для ответа.");
    if (Number(policy?.timeout_seconds) > 0) parts.push((mode === "autonomous" ? "Стартовое время: " : "Время работы: до ") + Math.ceil(policy.timeout_seconds / 60) + " мин.");
    $("mode-autonomous").disabled = state.status?.limits?.autonomous_enabled === false;
    $("effort-description").textContent = parts.join(" ");
  }
  function sourceName(id) {
    const match = indexes().find((item) => item.id === id);
    return match?.name || "Источник";
  }
  function updateSubmit() {
    const disabled = state.submitting || state.needsAuth || !state.status || state.status.enabled === false || conversationState.loading || Boolean(activeConversationRun());
    $("submit-question").disabled = disabled;
    $("submit-question").firstElementChild.textContent = state.submitting ? "Отправляем…" : "Отправить";
    const scopeLoading = Boolean(state.current && !Array.isArray(state.current.index_ids));
    $("deepen-run").disabled = disabled || scopeLoading;
    $("resume-run").disabled = disabled || scopeLoading;
    $("remember-run").disabled = state.needsAuth || scopeLoading;
    const cancelling = state.cancelling || state.current?.status === "cancelling";
    $("cancel-run").disabled = cancelling;
    $("cancel-run").textContent = cancelling ? "Останавливаем…" : "Остановить";
  }
  function renderSources() {
    const available = indexes();
    const known = new Set(available.map((item) => item.id));
    if (!state.sourcesInitialized) {
      state.selected = new Set(available.map((item) => item.id));
      state.sourcesInitialized = true;
    } else state.selected = new Set([...state.selected].filter((id) => known.has(id)));
    const fragment = document.createDocumentFragment();
    for (const item of available) {
      const label = element("label", "source-option");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selected.has(item.id);
      checkbox.value = item.id;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selected.add(item.id); else state.selected.delete(item.id);
        updateSourceCount();
      });
      label.append(checkbox, element("span", "", item.name || item.id));
      fragment.append(label);
    }
    $("source-list").replaceChildren(fragment);
    $("sources-empty").hidden = available.length > 0;
    $("sources-empty").textContent = "Доступные базы знаний пока не подключены.";
    updateSourceCount();
  }
  function updateSourceCount() {
    const total = indexes().length;
    $("source-count").textContent = state.selected.size + " / " + total;
    $("select-all-sources").checked = total > 0 && state.selected.size === total;
    $("select-all-sources").indeterminate = state.selected.size > 0 && state.selected.size < total;
    $("select-all-sources").setAttribute("aria-checked", $("select-all-sources").indeterminate ? "mixed" : String($("select-all-sources").checked));
    $("select-all-sources").disabled = total === 0;
  }
  function renderStatus({ refreshSources = true } = {}) {
    const status = state.status;
    const sharedIdentity = status.identity_scope === "shared";
    $("identity-label").textContent = (sharedIdentity ? "Общие чаты" : "Личные чаты") + " · общая память АС";
    $("history-title").textContent = sharedIdentity ? "Общие чаты" : "Ваши чаты";
    $("history-list").setAttribute("aria-label", sharedIdentity ? "Общие чаты" : "Ваши чаты");
    $("history-empty").textContent = sharedIdentity ? "Чаты видны всем, кто входит через эту общую учётную запись." : "Начните разговор о системе. Вопросы, ответы и исследования сохранятся в одном чате.";
    let notice = "";
    if (status.enabled === false) {
      notice = "Harness сейчас выключен. Сохранённые запросы остаются в истории.";
      connection("Выключено", "warning");
    } else if (status.source_available === false || status.sources_available === false || !indexes().length) {
      notice = "Источники пока недоступны. Запрос не сможет получить подтверждения, пока доступ не восстановлен.";
      connection("Нет источников", "warning");
    } else if (!status.model?.available) {
      notice = "Модель сейчас недоступна. Можно найти и прочитать источники; готовый ответ по ним не будет сгенерирован.";
      connection("Поиск по источникам", "warning");
    } else connection("Готов к работе", "ready");
    $("notice").textContent = notice;
    $("notice").hidden = !notice;
    let portalUrl = null;
    if (typeof status.portal_url === "string" && status.portal_url) {
      try { portalUrl = new URL(status.portal_url, document.baseURI).href; } catch (_) { /* Invalid link. */ }
    }
    $("portal-link").hidden = !link($("portal-link"), portalUrl);
    if (refreshSources) renderSources();
    renderEffortDescription();
    renderModel();
    updateSubmit();
  }
  function modelState() {
    const model = state.status?.model;
    if (model?.available === true) return "ready";
    return ["disabled", "starting", "authentication_required", "error"].includes(model?.state) ? model.state : "disabled";
  }
  function renderModel() {
    const model = state.status?.model || {};
    const direct = model.provider === "openai";
    $("model-heading").textContent = direct ? model.name || "Модель" : "GigaCode";
    const current = modelState();
    const canManage = (direct ? settingsState.canManage : state.modelDetails?.can_manage === true) && !state.needsAuth;
    $("control-open").hidden = state.status?.can_manage_control !== true || state.needsAuth;
    const labels = { disabled: "Выключен", starting: "Запускается", authentication_required: "Нужен вход", ready: "Готов", error: "Ошибка запуска" };
    const messages = {
      disabled: direct ? "Подключите модель в настройках, чтобы получать ответы на основе найденных материалов." : "Подключите GigaCode, чтобы получать ответы на основе найденных материалов.",
      starting: "Запускаем общий исполнитель. Состояние обновится автоматически.",
      authentication_required: canManage ? "Войдите в GigaCode, чтобы завершить подключение." : "Для подключения модели администратору нужно войти в GigaCode.",
      ready: "Модель готова к работе с вашими запросами.",
      error: direct ? "Модель недоступна. Проверьте подключение в настройках." : "GigaCode не удалось запустить. Можно повторить подключение.",
    };
    $("model-state-badge").textContent = state.status ? labels[current] : "Проверяем";
    $("model-state-badge").className = "model-state-badge " + (current === "ready" ? "ready" : current === "error" ? "error" : MODEL_PENDING.has(current) ? "pending" : "");
    $("model-message").textContent = !state.status ? "Проверяем готовность модели." :
      typeof model.message === "string" && model.message ? model.message :
      typeof model.reason === "string" && model.reason ? model.reason : messages[current];
    $("model-manager-hint").hidden = !state.status || canManage || current === "ready" || state.modelEndpointAvailable === null;
    $("model-manager-hint").textContent = direct ? "Попросите администратора проверить подключение модели в настройках." : current === "authentication_required" ?
      "Попросите администратора завершить вход в GigaCode." : "Для включения GigaCode обратитесь к администратору.";
    $("model-error").textContent = state.modelError;
    $("model-error").hidden = !state.modelError;
    $("model-actions").hidden = direct || !canManage || state.modelEndpointAvailable === false;
    const busy = Boolean(state.modelBusy);
    $("model-enable").hidden = current === "ready" || current === "authentication_required";
    $("model-enable").disabled = busy || current === "starting";
    $("model-enable").textContent = state.modelBusy === "enable" || current === "starting" ? "Запускаем…" :
      current === "error" ? "Повторить запуск" : "Включить GigaCode";
    $("model-disable").hidden = !(model.enabled === true || current === "ready" || MODEL_PENDING.has(current));
    $("model-disable").disabled = busy;
    $("model-disable").textContent = state.modelBusy === "disable" ? "Выключаем…" : "Выключить";
    $("model-shared-note").hidden = $("model-disable").hidden;
    $("model-auth-link").removeAttribute("href");
    $("model-auth-link").hidden = !(canManage && current === "authentication_required" &&
      !direct && link($("model-auth-link"), state.modelDetails?.authentication_url));
    // Privileged links and launch events never come from the public model status.
    const events = !direct && canManage && Array.isArray(state.modelDetails?.events) ? state.modelDetails.events.slice(-5) : [];
    $("model-events").hidden = events.length === 0;
    $("model-events-list").replaceChildren(...events.filter((event) => event && typeof event.message === "string").map((event) => {
      const item = element("li");
      if (event.timestamp) item.append(element("time", "", dateLabel(event.timestamp, true)));
      item.append(element("span", "", event.message));
      return item;
    }));
  }
  function applyModelDetails(details) {
    state.modelDetails = details?.can_manage === true ? details : { can_manage: false };
    // Only copy public fields into the workspace status; never auth URLs or events.
    if (state.status && state.status.model?.provider !== "openai" && details?.can_manage === true) {
      const model = { ...(state.status.model || {}) };
      for (const key of ["state", "enabled", "available", "message"]) {
        if (Object.prototype.hasOwnProperty.call(details, key)) model[key] = details[key];
      }
      state.status.model = model;
    }
  }
  function stopModelPolling() {
    window.clearTimeout(state.modelPollTimer);
    state.modelPollTimer = null;
    if (state.modelPollController) state.modelPollController.abort();
    state.modelPollController = null;
  }
  function scheduleModelPoll() {
    window.clearTimeout(state.modelPollTimer);
    if (!document.hidden && !state.needsAuth && !state.modelBusy && state.status &&
        state.status.model?.provider !== "openai" &&
        state.modelEndpointAvailable !== false && MODEL_PENDING.has(modelState())) {
      state.modelPollTimer = window.setTimeout(() => refreshModel(), 2000);
    }
  }
  async function refreshModel({ refreshPublic = true } = {}) {
    if (state.status?.model?.provider === "openai") { renderModel(); return; }
    if (document.hidden || state.needsAuth || !state.status || state.modelPollController ||
        state.modelBusy || state.modelEndpointAvailable === false) return;
    const controller = new AbortController();
    const identityEpoch = state.identityEpoch;
    const wasAvailable = state.status.model?.available === true;
    state.modelPollController = controller;
    try {
      const [details, status] = await Promise.all([
        api("model", { signal: controller.signal }),
        refreshPublic ? api("status", { signal: controller.signal }) : Promise.resolve(null),
      ]);
      if (controller.signal.aborted || identityEpoch !== state.identityEpoch) return;
      if (status) state.status = status;
      state.modelEndpointAvailable = true;
      state.modelError = "";
      applyModelDetails(details);
      renderStatus({ refreshSources: false });
      if (!wasAvailable && state.status.model?.available === true) loadWorkspace({ refreshModelState: false });
    } catch (error) {
      if (controller.signal.aborted || identityEpoch !== state.identityEpoch || error.name === "AbortError") return;
      if (error.status === 404) {
        state.modelEndpointAvailable = false;
        state.modelDetails = null;
        state.modelError = "";
      } else if (error.status === 403) {
        state.modelDetails = { can_manage: false };
        state.modelEndpointAvailable = true;
        state.modelError = "";
      } else if (error.status !== 401) state.modelError = "Не удалось обновить состояние GigaCode. " + errorMessage(error);
      renderModel();
    } finally {
      if (state.modelPollController === controller) state.modelPollController = null;
      if (identityEpoch === state.identityEpoch && !controller.signal.aborted) scheduleModelPoll();
    }
  }
  async function changeModel(action) {
    if (state.status?.model?.provider === "openai" || state.modelDetails?.can_manage !== true || state.modelBusy || state.needsAuth ||
        !["enable", "disable"].includes(action)) return;
    stopModelPolling();
    const controller = new AbortController();
    const identityEpoch = state.identityEpoch;
    state.modelActionController = controller;
    state.modelBusy = action;
    state.modelError = "";
    renderModel();
    try {
      const details = await api("model/" + action, { method: "POST", body: {}, signal: controller.signal });
      if (controller.signal.aborted || identityEpoch !== state.identityEpoch) return;
      state.modelEndpointAvailable = true;
      applyModelDetails(details);
      renderStatus({ refreshSources: false });
      if (action === "disable" || state.status?.model?.available === true) loadWorkspace({ refreshModelState: false });
    } catch (error) {
      if (controller.signal.aborted || identityEpoch !== state.identityEpoch || error.name === "AbortError") return;
      if (error.status === 404) { state.modelEndpointAvailable = false; state.modelDetails = null; }
      else if (error.status === 403) state.modelDetails = { can_manage: false };
      if (error.status !== 401) state.modelError = errorMessage(error);
    } finally {
      if (state.modelActionController === controller) state.modelActionController = null;
      if (identityEpoch === state.identityEpoch) {
        state.modelBusy = null;
        renderModel();
        scheduleModelPoll();
      }
    }
  }
  function rememberRun(run) {
    const id = idOf(run);
    if (!id) return;
    const known = state.runs.some((item) => idOf(item) === id);
    state.runs = [run, ...state.runs.filter((item) => idOf(item) !== id)]
      .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    const conversationId = String(run.conversation_id || run.parent_run_id || id);
    if (conversationState.id === conversationId) {
      const index = conversationState.runs.findIndex((item) => idOf(item) === id);
      if (index >= 0) conversationState.runs[index] = run;
      else { conversationState.runs.push(run); conversationState.total += 1; }
    }
    const previous = conversationState.list.find((item) => item.id === conversationId);
    const latest = conversationState.id === conversationId ? conversationState.runs.at(-1) || run : run;
    const entry = { ...previous, id: conversationId, conversation_id: conversationId,
      title: previous?.title || (conversationState.id === conversationId ? conversationState.runs[0]?.question : "") || run.question,
      created_at: previous?.created_at || run.created_at, updated_at: latest.updated_at || latest.created_at,
      status: latest.status, last_run_id: idOf(latest), run_count: Math.max(conversationState.id === conversationId ? conversationState.total : 1,
        (previous?.run_count || 0) + (!known && previous && previous.last_run_id !== id ? 1 : 0)) };
    conversationState.list = [entry, ...conversationState.list.filter((item) => item.id !== conversationId)]
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
    renderHistory();
  }
  function renderHistory() {
    $("history-more").hidden = conversationState.nextOffset === null;
    $("history-more").disabled = conversationState.listLoading;
    $("history-more").textContent = conversationState.listLoading ? "Загружаем…" : "Ещё чаты";
    const signature = JSON.stringify([conversationState.id, conversationState.list]);
    if (signature === state.historySignature) return;
    state.historySignature = signature;
    const fragment = document.createDocumentFragment();
    for (const run of conversationState.list) {
      const row = element("div", "history-row");
      const button = element("button", "history-item");
      button.type = "button";
      button.setAttribute("aria-current", String(run.id === conversationState.id));
      button.title = run.title || run.question || "Чат";
      const meta = element("span", "history-item-meta");
      const kind = ACTIVE.has(run.status) ? "active" : run.status === "completed" ? "completed" :
        ["failed", "timed_out"].includes(run.status) ? "failed" : "";
      const dot = element("span", "history-state-dot " + kind);
      dot.setAttribute("aria-hidden", "true");
      meta.append(dot, document.createTextNode(dateLabel(run.updated_at || run.created_at) + " · Запросов: " + (run.run_count || run.turn_count || 1)));
      button.append(element("span", "history-item-title", run.title || run.question || "Чат"), meta);
      button.addEventListener("click", () => loadConversation(run));
      const remove = element("button", "history-delete", "×"); remove.type = "button";
      remove.setAttribute("aria-label", "Удалить чат: " + (run.title || run.question || "Чат"));
      remove.title = "Удалить чат";
      remove.addEventListener("click", () => openDeletion("conversation", run));
      row.append(button, remove); fragment.append(row);
    }
    $("history-list").replaceChildren(fragment);
    $("history-count").textContent = String(conversationState.list.length);
    $("history-empty").hidden = conversationState.list.length > 0;
  }
  async function loadConversations(append = false) {
    if (state.needsAuth || (append && (conversationState.listLoading || conversationState.nextOffset === null))) return;
    const offset = append ? conversationState.nextOffset : 0;
    conversationState.listController?.abort();
    const controller = new AbortController(), epoch = state.identityEpoch;
    conversationState.listController = controller; conversationState.listLoading = true; renderHistory();
    try {
      const result = await api("conversations?limit=100&offset=" + offset, { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch) return;
      const entries = Array.isArray(result.conversations) ? result.conversations : [];
      conversationState.list = [...new Map([...(append ? conversationState.list : []), ...entries].map((item) => [item.id, item])).values()];
      conversationState.nextOffset = Number.isInteger(result.next_offset) ? result.next_offset : null;
    } catch (error) { if (!controller.signal.aborted && error.status !== 401 && epoch === state.identityEpoch) showError(errorMessage(error)); }
    finally { if (conversationState.listController === controller) { conversationState.listController = null; conversationState.listLoading = false; renderHistory(); } }
  }
  async function loadConversation(conversation, previous = false) {
    if (!conversation?.id || state.needsAuth || (previous && (conversationState.loading || conversationState.offset <= 0))) return;
    conversationState.controller?.abort();
    const sameConversation = conversationState.id === conversation.id, selectedId = sameConversation ? idOf(state.current) : null;
    if (!previous) {
      stopPolling(); state.generation += 1;
      if (!sameConversation) {
        resetRunFeedback(); state.current = null;
        Object.assign(conversationState, { id: conversation.id, title: conversation.title || conversation.question || "Чат", runs: [], total: 0, offset: 0, truncated: false, pollError: "", signature: null });
      }
    }
    const controller = new AbortController(), epoch = state.identityEpoch, generation = state.generation;
    conversationState.controller = controller; conversationState.loading = true;
    renderRun();
    const scroll = $("chat-scroll"), previousHeight = scroll.scrollHeight || 0, previousTop = scroll.scrollTop || 0;
    const offset = previous ? Math.max(0, conversationState.offset - 200) : 0;
    const limit = previous ? Math.min(200, conversationState.offset) : 200;
    const valid = () => !controller.signal.aborted && epoch === state.identityEpoch && generation === state.generation && conversationState.id === conversation.id;
    try {
      let result = await api("conversations/" + encodeURIComponent(conversation.id) + "?offset=" + offset + "&limit=" + limit, { signal: controller.signal });
      if (!valid()) return;
      if (!previous && result.has_more && result.total_runs > 200) {
        result = await api("conversations/" + encodeURIComponent(conversation.id) + "?offset=" + (result.total_runs - 200) + "&limit=200", { signal: controller.signal });
        if (!valid()) return;
      }
      const runs = Array.isArray(result.runs) ? result.runs : [];
      conversationState.runs = [...new Map([...runs, ...(previous ? conversationState.runs : [])].map((run) => [idOf(run), run])).values()];
      conversationState.total = result.total_runs || conversationState.runs.length;
      conversationState.offset = result.offset || 0;
      conversationState.truncated = result.history_truncated === true;
      conversationState.title = result.conversation?.title || conversationState.title;
      state.runs = [...new Map([...state.runs, ...runs].map((run) => [idOf(run), run])).values()];
      if (!previous) {
        state.current = conversationState.runs.find((run) => idOf(run) === selectedId) || conversationState.runs.at(-1) || null;
        if (!sameConversation) {
          const latest = conversationState.runs.at(-1);
          const modeInput = latest && ["quick", "investigate", "autonomous"].includes(latest.mode) ? document.querySelector('input[name="mode"][value="' + latest.mode + '"]') : null;
          if (modeInput) modeInput.checked = true;
          if (latest?.effort) $("effort").value = latest.effort;
          renderEffortDescription();
        }
        if (state.current) { state.selected = new Set(state.current.index_ids || []); state.selectedSandboxProfile = state.current.sandbox_profile_id || ""; renderSources(); renderExecutionEnvironment(); }
        $("composer-options").open = false;
      }
      conversationState.pollError = ""; showError(""); renderRun(); schedulePoll();
      if (previous) scroll.scrollTop = previousTop + (scroll.scrollHeight || 0) - previousHeight;
      else scrollChatToLatest();
    } catch (error) { if (valid() && error.status !== 401) showError(errorMessage(error)); }
    finally { if (conversationState.controller === controller) { conversationState.controller = null; conversationState.loading = false; renderTranscript(); updateSubmit(); schedulePoll(); } }
  }
  function activeConversationRun() {
    return [...conversationState.runs].reverse().find((run) => ACTIVE.has(run.status)) || (ACTIVE.has(state.current?.status) ? state.current : null);
  }
  function renderTranscript() {
    const panel = $("run-panel"), hasConversation = Boolean(conversationState.id);
    const scroll = $("chat-scroll"), previousTop = scroll.scrollTop || 0;
    const wasAtBottom = (scroll.scrollHeight || 0) - (scroll.clientHeight || 0) - previousTop < 80;
    $("conversation-panel").hidden = !hasConversation;
    $("conversation-title").textContent = conversationState.title || "Чат";
    $("conversation-loading").hidden = !conversationState.loading;
    $("conversation-history-note").hidden = !conversationState.truncated;
    $("conversation-sync").textContent = conversationState.pollError;
    $("conversation-sync").hidden = !conversationState.pollError;
    $("conversation-more").hidden = conversationState.offset <= 0;
    $("conversation-more").disabled = conversationState.loading;
    $("delete-conversation").disabled = conversationState.loading;
    $("refresh-conversation").disabled = conversationState.loading;
    $("composer-section").classList.toggle("in-conversation", hasConversation);
    $("question").placeholder = hasConversation ? "Уточните вопрос или продолжите исследование…" : "Что хотите узнать о системе?";
    const signature = JSON.stringify([idOf(state.current), conversationState.runs.map((run) => [idOf(run), run.question, run.answer, run.phase, run.status, run.stop_reason, run.evidence?.length, run.research_plan, run.memory_used, run.current_action, run.research_trace?.at(-1)])]);
    if (signature === conversationState.signature) return;
    conversationState.signature = signature;
    const fragment = document.createDocumentFragment();
    for (const run of conversationState.runs) {
      const turn = element("article", "chat-turn"); turn.dataset.runId = idOf(run);
      const user = element("div", "chat-user");
      user.append(element("span", "chat-role", "Вы"), element("p", "chat-user-text", run.question || "Сообщение"));
      const assistant = element("div", "chat-assistant");
      const heading = element("div", "chat-assistant-heading");
      heading.append(element("span", "chat-role", "Ответ Harness"), element("span", "chat-status" + (ACTIVE.has(run.status) ? " is-active" : ""), runStatusLabel(run)));
      assistant.append(heading);
      if (idOf(run) === idOf(state.current)) assistant.append(panel);
      else {
        if (ACTIVE.has(run.status)) assistant.append(element("p", "chat-turn-status", actionDescription(run.current_action) || PHASE_LABELS[run.phase] || STATUS_LABELS[run.status]));
        const answer = element("div", "answer chat-answer"); renderAnswer(typeof run.answer === "string" ? run.answer : "", answer); assistant.append(answer);
        const inspect = element("button", "text-button chat-inspect", "Показать ход работы и материалы"); inspect.type = "button";
        inspect.addEventListener("click", () => selectRun(run)); assistant.append(inspect);
      }
      turn.append(user, assistant); fragment.append(turn);
    }
    if (!state.current) { panel.hidden = true; fragment.append(panel); }
    $("conversation-messages").replaceChildren(fragment);
    if (wasAtBottom && hasConversation) scrollChatToLatest();
    else scroll.scrollTop = previousTop;
  }
  function renderEvidence(evidence) {
    const items = Array.isArray(evidence) ? evidence : [];
    const signature = JSON.stringify(items);
    if (signature === state.evidenceSignature) return;
    state.evidenceSignature = signature;
    const openIds = new Set([...$("evidence-list").querySelectorAll("details[open]")].map((item) => item.dataset.id));
    const fragment = document.createDocumentFragment();
    items.forEach((item, index) => {
      const card = element("article", "evidence-item");
      const label = element("div", "evidence-label");
      label.append(element("span", "evidence-number", String(index + 1)), document.createTextNode(sourceName(item.index_id)));
      const title = element(safeUrl(item.url) ? "a" : "div", "evidence-title", item.title || "Материал");
      if (title.tagName === "A") link(title, item.url);
      card.append(label, title);
      const text = typeof item.text === "string" ? item.text : "";
      if (text.length <= 260) card.append(element("p", "evidence-preview", text));
      else {
        card.append(element("p", "evidence-preview", text.slice(0, 230).trimEnd() + "…"));
        const details = element("details", "evidence-details");
        details.dataset.id = String(item.id || index);
        details.open = openIds.has(details.dataset.id);
        details.append(element("summary", "", "Прочитать выдержку"), element("p", "", text));
        card.append(details);
      }
      if (item.revision) card.append(element("span", "evidence-revision", "Версия: " + item.revision));
      fragment.append(card);
    });
    $("evidence-list").replaceChildren(fragment);
    $("evidence-count").textContent = String(items.length);
    $("evidence-empty").hidden = items.length > 0;
    $("evidence-empty").textContent = state.current && !ACTIVE.has(state.current.status) ?
      "В этом запросе подтверждения не найдены." : "Здесь появятся материалы, найденные для вашего запроса.";
  }
  // markdown-it owns parsing; construct DOM from its tokens, never model-supplied HTML.
  const markdown = typeof window.markdownit === "function" ? window.markdownit({ html: false, linkify: true, breaks: false }) : null;
  const markdownTags = new Set(["p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "em", "strong", "s", "table", "thead", "tbody", "tr", "th", "td"]);
  function appendMarkdown(target, tokens) {
    const stack = [target];
    for (const token of tokens) {
      const parent = stack[stack.length - 1];
      if (token.type === "inline") { appendMarkdown(parent, token.children || []); continue; }
      if (token.type === "text" || token.type === "html_inline" || token.type === "html_block") { parent.append(document.createTextNode(token.content)); continue; }
      if (token.type === "softbreak" || token.type === "hardbreak") { parent.append(token.type === "hardbreak" ? element("br") : document.createTextNode("\n")); continue; }
      if (token.type === "code_inline") { parent.append(element("code", "", token.content)); continue; }
      if (token.type === "fence" || token.type === "code_block") {
        const block = element("div", "code-block"), language = String(token.info || "").trim().split(/\s/)[0];
        if (language) block.append(element("div", "code-language", language.slice(0, 48)));
        const pre = element("pre"); pre.append(element("code", "", token.content.replace(/\n$/, ""))); block.append(pre); parent.append(block); continue;
      }
      if (token.type === "image") {
        // Remote images are links, so generated answers cannot silently load tracking resources.
        const url = token.attrGet("src"), label = token.content || "Изображение";
        if (safeUrl(url)) { const anchor = element("a", "answer-image-link", label); link(anchor, url); parent.append(anchor); }
        else parent.append(document.createTextNode(label));
        continue;
      }
      if (token.type === "hr") { parent.append(element("hr")); continue; }
      if (token.nesting === -1) { if (stack.length > 1) stack.pop(); continue; }
      if (token.nesting === 1) {
        let node;
        if (token.type === "link_open") {
          const url = token.attrGet("href"); node = element(safeUrl(url) ? "a" : "span");
          if (safeUrl(url)) link(node, url);
        } else node = element(markdownTags.has(token.tag) ? token.tag : "span");
        if (token.tag === "ol" && /^\d+$/.test(token.attrGet("start") || "")) node.setAttribute("start", token.attrGet("start"));
        if (["th", "td"].includes(token.tag)) {
          const alignment = /^text-align:(left|right|center)$/.exec(token.attrGet("style") || "");
          if (alignment) node.className = "align-" + alignment[1];
        }
        if (token.hidden) node.className = "markdown-tight";
        if (token.tag === "table") { const scroll = element("div", "answer-table"); scroll.tabIndex = 0; scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", "Таблица в ответе"); scroll.append(node); parent.append(scroll); }
        else parent.append(node);
        stack.push(node);
      }
    }
  }
  function renderAnswer(text, target = $("answer")) {
    const selected = target === $("answer");
    if (selected && text === state.answerSignature) return;
    if (selected) state.answerSignature = text;
    const fragment = document.createDocumentFragment();
    if (markdown) appendMarkdown(fragment, markdown.parse(text, {}));
    else fragment.append(element("p", "answer-plain", text));
    target.replaceChildren(fragment);
    if (selected && text) appear(target);
  }
  function expandFeedback(open) {
    $("feedback-comment-details").hidden = !open;
    $("feedback-toggle").setAttribute("aria-expanded", String(open));
  }
  function resetRunFeedback() {
    feedbackState.controller?.abort();
    Object.assign(feedbackState, { controller: null, busy: false, runId: null, message: "" });
    $("run-feedback-comment").value = "";
    expandFeedback(false);
    $("run-feedback-status").textContent = "";
    $("run-feedback").hidden = true;
  }
  function renderRunFeedback(run) {
    const visible = Boolean(run && !ACTIVE.has(run.status) && run.answer && Array.isArray(run.index_ids));
    $("run-feedback").hidden = !visible || state.needsAuth;
    if (!visible) return;
    if (feedbackState.runId !== idOf(run)) {
      feedbackState.runId = idOf(run);
      feedbackState.message = "";
      $("run-feedback-comment").value = run.feedback?.comment || "";
      expandFeedback(false);
    }
    for (const rating of ["helpful", "unhelpful"]) {
      const button = $("feedback-" + rating);
      button.disabled = feedbackState.busy || state.needsAuth;
      button.setAttribute("aria-pressed", String(run.feedback?.rating === rating));
    }
    $("run-feedback-comment").disabled = feedbackState.busy;
    $("feedback-save-comment").hidden = !run.feedback?.rating;
    $("feedback-save-comment").disabled = feedbackState.busy ||
      $("run-feedback-comment").value.trim() === (run.feedback?.comment || "");
    $("run-feedback-status").textContent = feedbackState.busy ? "Сохраняем оценку…" : feedbackState.message ||
      (run.feedback?.rating ? "Оценка сохранена." : "Выберите «Да» или «Нет», чтобы отправить оценку с комментарием.");
  }
  async function saveRunFeedback(rating) {
    const run = state.current;
    if (!run || ACTIVE.has(run.status) || feedbackState.busy || state.needsAuth) return;
    const id = idOf(run), epoch = state.identityEpoch;
    // A response fetched before this mutation must not replace the saved rating.
    stopPolling();
    const generation = ++state.generation;
    const controller = new AbortController();
    feedbackState.controller = controller; feedbackState.busy = true; feedbackState.message = "";
    const body = { rating, comment: $("run-feedback-comment").value.trim() };
    renderRunFeedback(run);
    try {
      const response = await api("runs/" + encodeURIComponent(id) + "/feedback", { method: "PUT", body, signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch || generation !== state.generation || id !== idOf(state.current)) return;
      state.current.feedback = response.feedback;
      feedbackState.message = response.memory ? "Оценка сохранена. Общий опыт АС обновлён для следующих похожих запросов." : "Оценка сохранена в вашем чате.";
      loadMemory();
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401 && epoch === state.identityEpoch && generation === state.generation && id === idOf(state.current)) {
        feedbackState.message = errorMessage(error);
        expandFeedback(true);
      }
    } finally {
      if (feedbackState.controller === controller) {
        feedbackState.controller = null; feedbackState.busy = false;
        if (epoch === state.identityEpoch && generation === state.generation) renderRunFeedback(state.current);
      }
    }
  }
  const ACTION_LABELS = {
    update_research_plan: "Обновление плана", request_budget: "Запрос дополнительных ресурсов",
    budget_review: "Проверка необходимого бюджета", research_round: "Проверка результатов поиска",
    goal_check: "Проверка оставшихся вопросов", answer_recovery: "Повторная подготовка ответа",
    answer_quality_check: "Проверка ответа по источникам", answer_quality_rejected: "Черновик не прошёл проверку",
    research_recovery: "Поиск дополнительных подтверждений",
    model_policy_stop: ANSWER_STOP_LABELS.model_policy_stop, model_refusal_text: ANSWER_STOP_LABELS.model_refusal_text,
    model_planning: "Модель выбирает следующий шаг", model_budget_review: "Модель оценивает бюджет",
    model_retry: "Повторный запрос к модели", model_response: "Модель анализирует материалы и готовит ответ",
    model_received: "Получен ответ модели", model_call: "Ожидаем ответ модели",
    context_compaction: "Обновляем сводку беседы", compact_conversation: "Сохранение сводки беседы",
    conversation_compaction: "Сохранение сводки беседы", mcp_list_tools: "Изучаем доступные инструменты",
    mcp_tool_schema: "Проверяем параметры инструмента",
    search_repository: "Поиск в репозитории", read_repository: "Чтение файла в репозитории",
  };
  function actionLabel(name) {
    if (!name) return "";
    if (Object.hasOwn(ACTION_LABELS, name)) return ACTION_LABELS[name];
    if (/search.*(knowledge|index|document)|^search$/.test(name)) return "Поиск в базе знаний";
    if (/read.*document/.test(name)) return "Чтение документа";
    if (/clone|prepare.*repository/.test(name)) return "Подготовка репозитория";
    if (/search.*(code|repository|repo)|read.*(code|file)|repository/.test(name)) return "Изучение кода сервиса";
    if (/execute|sandbox|run_code|run_command/.test(name)) return "Проверка в песочнице";
    if (/a2a|agent/.test(name)) return "Запрос подключённому агенту";
    if (/mcp/.test(name)) return "Запрос подключённому инструменту";
    return "Шаг исследования";
  }
  function actionDescription(action) {
    if (!action?.name) return "";
    const ids = action.args || {};
    const scope = ids.index_id ? sourceName(ids.index_id) : ids.repository_id || ids.connection_id;
    return [actionLabel(action.name), scope, ids.document_id].filter(Boolean).join(" · ");
  }
  function runStatusLabel(run) {
    if (["partial", "failed"].includes(run.status) && Object.hasOwn(ANSWER_STOP_LABELS, run.stop_reason)) return ANSWER_STOP_LABELS[run.stop_reason];
    if (run.status === "partial" && !run.evidence?.length) return "Недостаточно данных";
    return STATUS_LABELS[run.status] || "Состояние запроса обновляется";
  }
  function renderResearchActivity(run) {
    const plan = run.research_plan && typeof run.research_plan === "object" ? run.research_plan : {};
    const trace = Array.isArray(run.research_trace) ? run.research_trace.filter((item) => item && typeof item.name === "string") : [];
    const strings = (items) => Array.isArray(items) ? items.filter((item) => typeof item === "string").slice(0, 16) : [];
    const questions = strings(plan.questions), completed = strings(plan.completed), gaps = strings(plan.gaps);
    const currentAction = ACTIVE.has(run.status) && typeof run.current_action?.name === "string" ? run.current_action.name : "";
    $("research-activity").hidden = !trace.length && !plan.goal && !questions.length && !gaps.length && !currentAction;
    $("research-goal").textContent = typeof plan.goal === "string" ? "Цель: " + plan.goal : "";
    $("research-goal").hidden = !plan.goal;
    const next = ACTIVE.has(run.status) && typeof plan.next_action === "string" ? plan.next_action :
      !ACTIVE.has(run.status) && typeof plan.completion_reason === "string" ? plan.completion_reason : "";
    $("research-next").textContent = (ACTIVE.has(run.status) ? "Следующий шаг: " : "Результат проверки: ") + next;
    $("research-next").hidden = !next;
    const blocks = [];
    if (Number.isInteger(plan.round) && plan.round > 0) blocks.push(element("p", "research-round", "Раунд исследования: " + plan.round));
    for (const [label, values] of [["Вопросы исследования", questions], ["Проверено", completed], ["Осталось выяснить", gaps]]) {
      if (!values.length) continue;
      const block = element("section", "research-plan-group"), list = element("ul");
      block.append(element("h4", "", label));
      for (const value of values) list.append(element("li", "", value));
      block.append(list); blocks.push(block);
    }
    $("research-plan").replaceChildren(...blocks);
    $("research-current").textContent = currentAction ? "Сейчас: " + actionDescription(run.current_action) : "";
    $("research-current").hidden = !currentAction;
    $("research-timeline").replaceChildren(...trace.reverse().map((item) => {
      const row = element("li", "research-event");
      row.append(element("strong", "", actionDescription(item)));
      if (typeof item.result === "string" && item.result) row.append(element("p", "", item.result.slice(0, 400)));
      if (item.timestamp) row.append(element("time", "", dateLabel(item.timestamp, true)));
      return row;
    }));
    $("research-details").hidden = !trace.length && !blocks.length;
  }
  function renderBudget(run) {
    const budget = run.budget || {}, context = run.context_summary || {}, current = budget.current || {}, ceiling = budget.ceiling || {}, metrics = run.metrics || {};
    $("run-budget").hidden = !Object.keys(current).length && !context.tokens_before && !context.compactions;
    const format = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("ru-RU") : "—";
    const used = { model_calls: metrics.decision_calls ?? metrics.model_calls, summary_calls: metrics.summary_calls, tool_calls: metrics.tool_calls, source_calls: Math.max(0, (metrics.source_calls || 0) - (metrics.bootstrap_calls || 0)) };
    const labels = { model_calls: "Основные вызовы модели", summary_calls: "Подготовка сводки", source_calls: "Повторные поиски и чтения", tool_calls: "Инструменты", timeout_seconds: "Рабочее время, секунды", context_tokens: "Контекст, токены ≈", parallelism: "Параллельные обращения", document_reads: "Автоматическое чтение документов", evidence_per_index: "Материалы на индекс", evidence_chars: "Размер выдержки, символы", document_chars: "Размер документа, символы" };
    $("run-budget-values").replaceChildren(...Object.entries(labels).filter(([key]) => current[key] !== undefined).map(([key, label]) => {
      const card = element("div", "budget-value"); card.append(element("strong", "", label));
      card.append(element("span", "", (used[key] !== undefined ? "Использовано " + format(used[key]) + " · " : "") + "Бюджет " + format(current[key]) + (run.mode === "autonomous" ? " · Потолок " + format(ceiling[key]) : ""))); return card;
    }));
    $("run-context-summary").textContent = context.tokens_before !== undefined ? "Беседа: ≈ " + format(context.tokens_before) + " → " + format(context.tokens_after) + " токенов. Сжатий: " + format(context.compactions || 0) + (context.reused_summary ? ". Использована сохранённая сводка." : ".") : "";
    if (metrics.answer_retries) $("run-context-summary").textContent += " Дополнительных попыток восстановления ответа: " + format(metrics.answer_retries) + ".";
    $("run-budget-events").replaceChildren(...(Array.isArray(budget.events) ? budget.events : []).map((event) => {
      const row = element("li", "research-event"); row.append(element("strong", "", event.accepted ? "Бюджет изменён" : "Изменение не разрешено"));
      if (typeof event.reason === "string") row.append(element("p", "", event.reason));
      if (typeof event.message === "string") row.append(element("p", "", event.message));
      if (typeof event.next_action === "string" && event.next_action) row.append(element("p", "", "Следующий шаг: " + event.next_action));
      for (const [label, values] of [["Запрошено", event.requested], ["Разрешено", event.granted]]) if (values && typeof values === "object") row.append(element("p", "", label + ": " + Object.entries(values).map(([key, value]) => (labels[key] || key) + " " + format(value)).join(" · ")));
      if (event.timestamp) row.append(element("time", "", dateLabel(event.timestamp, true))); return row;
    }));
  }
  function renderRun() {
    const run = state.current;
    const hasRun = Boolean(run);
    const newPresentation = hasRun && idOf(run) !== state.presentedRunId;
    state.presentedRunId = hasRun ? idOf(run) : null;
    $("welcome").hidden = Boolean(conversationState.id);
    $("suggestions").hidden = Boolean(conversationState.id);
    $("run-panel").hidden = !hasRun;
    $("page-label").textContent = conversationState.id ? "Чат" : "Новый чат";
    if (!run) { renderEvidence([]); renderHistory(); $("discoveries-card").hidden = true; renderRunFeedback(null); renderTranscript(); updateSubmit(); return; }
    const active = ACTIVE.has(run.status);
    $("run-mode").textContent = modeLabel(run.mode) + " · Усилие: " + effortLabel(run.effort);
    $("run-date").textContent = dateLabel(run.created_at, true);
    $("run-question").textContent = run.question || "Запрос";
    $("parent-note").hidden = true;
    const evidenceCount = Array.isArray(run.evidence) ? run.evidence.length : 0;
    const status = runStatusLabel(run), currentAction = active ? actionDescription(run.current_action) : "";
    $("run-status").textContent = currentAction || status;
    $("run-progress").hidden = !active && run.status === "completed";
    const actionStart = Date.parse(run.current_action?.started_at || "");
    const elapsed = Number.isFinite(actionStart) ? Math.max(0, Math.floor((Date.now() - actionStart) / 1000)) : 0;
    $("run-waiting").hidden = !active || !Number.isFinite(actionStart);
    $("run-waiting").textContent = "Текущее действие: " + (elapsed >= 60 ? Math.floor(elapsed / 60) + " мин " + elapsed % 60 + " сек" : elapsed + " сек") +
      (["model_response", "model_call", "model_planning", "model_budget_review", "context_compaction"].includes(run.current_action?.name) ? " · Ожидаем ответ модели." : "");
    const trace = Array.isArray(run.research_trace) ? run.research_trace : [];
    const lastStep = trace.at(-1);
    const rejected = trace.findLast((item) => item?.name === "answer_quality_rejected");
    const showRejection = active && rejected && rejected !== lastStep;
    $("run-recovery-note").hidden = !showRejection;
    $("run-recovery-note").textContent = showRejection ? [actionLabel(rejected.name), rejected.result].filter(Boolean).join(" · ") : "";
    $("run-last-step").hidden = !active || !lastStep;
    $("run-last-step").textContent = lastStep ? "Последнее действие: " + actionDescription(lastStep) + (lastStep.result ? " · " + String(lastStep.result).slice(0, 180) : "") : "";
    if (newPresentation || $("run-details").dataset.active !== String(active)) $("run-details").open = false;
    $("run-details").dataset.active = String(active);
    $("run-details-label").textContent = active ? "План и ход исследования" : "Как получен ответ";
    $("run-details-summary").textContent = "Подтверждений: " + evidenceCount + (run.metrics?.tool_calls ? " · Действий: " + run.metrics.tool_calls : "");
    const phase = run.status === "cancelling" ? "Ожидаем завершения текущего действия." :
      active ? (PHASE_LABELS[run.phase] || "Ожидаем обновления от сервера.") :
      run.status === "partial" ? "Ответ может быть неполным. Проверьте найденные материалы и замечания." :
      ["failed", "interrupted", "timed_out"].includes(run.status) ? "Найденные до остановки материалы сохранены." : "";
    $("run-phase").textContent = phase;
    $("run-phase").hidden = !phase;
    $("run-status-dot").className = "run-status-dot " + (active ? "active" : run.status === "completed" ? "completed" : ["failed", "timed_out"].includes(run.status) ? "failed" : "");
    const coverage = run.coverage && typeof run.coverage === "object" ? Object.values(run.coverage) : [];
    const searched = coverage.filter((item) => item && ["searched", "empty"].includes(item.status)).length;
    const failed = coverage.filter((item) => item?.status === "failed").length;
    const pending = coverage.filter((item) => item?.status === "pending").length;
    $("run-coverage").hidden = !coverage.length;
    $("run-coverage").textContent = "Поиск по базам знаний: " + searched + " / " + coverage.length +
      (failed ? " · Недоступно: " + failed : "") + (pending ? " · Ещё не проверено: " + pending : "");
    renderResearchActivity(run);
    renderBudget(run);
    const announcement = [currentAction || status, phase, "Найдено подтверждений: " + evidenceCount].filter(Boolean).join(". ");
    if ($("progress-live").textContent !== announcement) $("progress-live").textContent = announcement;
    $("cancel-run").hidden = !active;
    $("deepen-run").hidden = active || ["failed", "interrupted"].includes(run.status);
    $("resume-run").hidden = active || run.resumable !== true;
    $("remember-run").hidden = active || !memoryState.available;
    const memories = Array.isArray(run.memory_used) ? run.memory_used : [];
    $("run-memory-used").replaceChildren(element("strong", "", "Учтено из общей памяти АС: " + memories.length), ...memories.map((item) => element("p", "", item.title || "Сохранённая запись")));
    if (run.memory_contribution?.updated) $("run-memory-used").append(element("p", "memory-contribution", "Опыт этого запроса добавлен в общую память АС. Оцените ответ, чтобы уточнить, что помогло и что нужно исправить."));
    $("run-memory-used").hidden = !memories.length && !run.memory_contribution?.updated;
    renderRunEnvironment(run);
    const warnings = Array.isArray(run.warnings) ? run.warnings.filter((item) => typeof item === "string") : [];
    $("run-warnings").replaceChildren(...warnings.map((text) => element("p", "", text)));
    $("run-warnings").hidden = !warnings.length;
    const answer = typeof run.answer === "string" ? run.answer : "";
    renderAnswer(answer);
    $("answer-placeholder").hidden = Boolean(answer) || !active;
    renderEvidence(run.evidence);
    const milliseconds = Number(run.metrics?.duration_ms);
    const metrics = [];
    if (Number.isFinite(milliseconds) && milliseconds > 0) metrics.push(Math.max(1, Math.round(milliseconds / 1000)) + " сек.");
    metrics.push("Подтверждений: " + evidenceCount);
    if (run.metrics?.document_reads !== undefined) metrics.push("Документов прочитано: " + run.metrics.document_reads);
    if (run.metrics?.tool_calls) metrics.push("Вызовов инструментов: " + run.metrics.tool_calls);
    if (memories.length) metrics.push("Из памяти: " + memories.length);
    $("run-metrics").textContent = metrics.join(" · ");
    renderRunFeedback(run);
    updateSubmit();
    renderHistory();
    renderTranscript();
    loadRunDiscoveries();
    const memorySignature = idOf(run) + ":" + run.status;
    if (!active && state.memoryRunSignature !== memorySignature) { state.memoryRunSignature = memorySignature; loadMemory(); }
    if (newPresentation) appear($("run-panel"));
  }
  function stopPolling() {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (state.pollController) state.pollController.abort();
    state.pollController = null;
  }
  function schedulePoll(delay = state.pollDelay) {
    window.clearTimeout(state.pollTimer);
    const active = activeConversationRun();
    if (!document.hidden && !state.needsAuth && active) {
      state.pollTimer = window.setTimeout(() => refreshRun(idOf(active), state.generation), delay);
    }
  }
  async function refreshRun(id, generation) {
    if (state.pollController || document.hidden) return;
    const controller = new AbortController(), epoch = state.identityEpoch;
    state.pollController = controller;
    try {
      const run = await api("runs/" + encodeURIComponent(id), { signal: controller.signal });
      if (controller.signal.aborted || epoch !== state.identityEpoch || generation !== state.generation ||
          (id !== idOf(state.current) && !conversationState.runs.some((item) => idOf(item) === id))) return;
      const selected = id === idOf(state.current), scopeWasLoading = selected && !Array.isArray(state.current.index_ids);
      if (selected) state.current = run;
      if (scopeWasLoading) {
        state.selected = new Set(run.index_ids || []); state.selectedSandboxProfile = run.sandbox_profile_id || "";
        renderSources(); renderExecutionEnvironment();
      }
      state.pollDelay = 1400;
      conversationState.pollError = "";
      rememberRun(run);
      if (selected) renderRun(); else { renderTranscript(); updateSubmit(); }
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401 && generation === state.generation) {
        conversationState.pollError = "Обновление временно недоступно. " + errorMessage(error) + " Повторим проверку автоматически.";
        renderTranscript();
        state.pollDelay = Math.min(10000, state.pollDelay * 2);
      }
    } finally {
      if (state.pollController === controller) state.pollController = null;
      if (generation === state.generation && !controller.signal.aborted) schedulePoll();
    }
  }
  function selectRun(run) {
    stopPolling();
    resetRunFeedback();
    state.generation += 1;
    const conversationId = String(run.conversation_id || run.parent_run_id || idOf(run));
    if (conversationState.id !== conversationId) {
      conversationState.controller?.abort();
      Object.assign(conversationState, { id: conversationId, title: run.question || "Чат", runs: [run], total: 1, offset: 0, truncated: false, pollError: "", loading: false, controller: null, signature: null });
    }
    state.current = run;
    if (Array.isArray(run.index_ids)) { state.selected = new Set(run.index_ids); renderSources(); }
    state.selectedSandboxProfile = run.sandbox_profile_id || ""; renderExecutionEnvironment();
    discoveryState.current = []; discoveryState.signature = null;
    $("discovery-feedback").textContent = ""; renderDiscoveries();
    state.pollDelay = 1400;
    showError("");
    renderRun();
    refreshRun(idOf(run), state.generation);
  }
  function resetWorkspace() {
    stopPolling();
    conversationState.controller?.abort();
    Object.assign(conversationState, { id: null, title: "", runs: [], total: 0, offset: 0, truncated: false, pollError: "", loading: false, controller: null, signature: null });
    $("composer-options").open = false;
    resetRunFeedback();
    state.generation += 1;
    state.current = null;
    state.selectedSandboxProfile = ""; renderExecutionEnvironment();
    discoveryState.controller?.abort(); discoveryState.current = []; discoveryState.signature = null;
    renderDiscoveries();
    showError("");
    $("question").value = "";
    updateCharacterCount();
    renderRun();
    $("chat-scroll").scrollTop = 0;
    $("question").focus();
  }
  function resetIdentity() {
    resetSettingsAccess();
    resetPersonalData();
    stopModelPolling();
    if (state.modelActionController) state.modelActionController.abort();
    state.modelActionController = null;
    state.modelDetails = null;
    state.modelEndpointAvailable = null;
    state.modelError = "";
    state.modelBusy = null;
    state.status = null;
    state.sourcesInitialized = false;
    state.runs = [];
    conversationState.listController?.abort();
    Object.assign(conversationState, { list: [], nextOffset: null, listLoading: false, listController: null });
    state.selected = new Set();
    state.submitting = false;
    state.cancelling = false;
    $("source-list").replaceChildren();
    $("source-count").textContent = "0 / 0";
    $("sources-empty").hidden = false;
    $("sources-empty").textContent = "Загружаем доступные источники…";
    $("select-all-sources").checked = false;
    $("select-all-sources").indeterminate = false;
    $("select-all-sources").setAttribute("aria-checked", "false");
    $("select-all-sources").disabled = true;
    $("notice").hidden = true;
    $("portal-link").hidden = true;
    resetWorkspace();
    renderModel();
    updateSubmit();
  }
  function updateCharacterCount() { $("character-count").textContent = $("question").value.length.toLocaleString("ru-RU") + " / 8 000"; }
  async function submitRun(parent = null) {
    if (state.submitting) return;
    if (state.needsAuth) { openAuth(); return; }
    if (conversationState.loading || activeConversationRun()) { showError("Дождитесь ответа или остановите текущее исследование."); return; }
    const followup = parent || conversationState.runs.at(-1) || null;
    const question = parent ? parent.question : $("question").value.trim();
    if (!question) { $("question").focus(); return; }
    const selected = parent ? (Array.isArray(parent.index_ids) ? parent.index_ids : []) : [...state.selected];
    if (!parent && indexes().length && !selected.length) {
      showError("Выберите хотя бы один источник в области поиска.");
      setPanelCollapsed("context", false);
      const summary = $("sources-disclosure").querySelector("summary");
      if (summary.getAttribute("aria-expanded") !== "true") summary.click();
      $("source-list").querySelector("input")?.focus();
      return;
    }
    const payload = {
      question,
      mode: parent ? (parent.mode === "autonomous" ? "autonomous" : "investigate") : document.querySelector('input[name="mode"]:checked').value,
      effort: parent ? "high" : $("effort").value,
      index_ids: selected,
      ...(followup ? { parent_run_id: idOf(followup) } : {}),
    };
    if (payload.mode === "autonomous" && state.status?.limits?.autonomous_enabled === false) { showError("Автопилот отключён администратором. Выберите другой режим."); return; }
    payload.sandbox_profile_id = payload.mode !== "quick" ? state.selectedSandboxProfile : "";
    if (payload.sandbox_profile_id && !state.sandboxProfiles.some((profile) => profile.id === payload.sandbox_profile_id)) {
      showError("Выбранная среда выполнения недоступна. Выберите другую среду или вариант по умолчанию."); return;
    }
    state.submitting = true;
    const identityEpoch = state.identityEpoch;
    const generation = state.generation;
    showError("");
    updateSubmit();
    try {
      const run = await api("runs", { method: "POST", body: payload });
      if (identityEpoch !== state.identityEpoch) return;
      rememberRun(run);
      if (generation !== state.generation) return;
      selectRun(run);
      $("composer-options").open = false;
      $("question").value = "";
      updateCharacterCount();
      scrollChatToLatest();
    } catch (error) {
      if (error.status !== 401 && identityEpoch === state.identityEpoch) {
        showError(error.uncertainMutation ? "Не получено подтверждение отправки. Запрос мог быть принят; проверьте историю чатов перед повторной отправкой." : errorMessage(error));
        if (error.uncertainMutation) loadConversations();
      }
    }
    finally { if (identityEpoch === state.identityEpoch) { state.submitting = false; updateSubmit(); } }
  }
  async function cancelRun() {
    if (!state.current || !ACTIVE.has(state.current.status) || state.cancelling) return;
    const id = idOf(state.current), generation = state.generation, identityEpoch = state.identityEpoch;
    state.cancelling = true;
    stopPolling();
    updateSubmit();
    try {
      const run = await api("runs/" + encodeURIComponent(id) + "/cancel", { method: "POST" });
      if (identityEpoch !== state.identityEpoch) return;
      rememberRun(run);
      if (generation === state.generation) { state.current = run; renderRun(); }
    } catch (error) { if (error.status !== 401 && identityEpoch === state.identityEpoch) showError(errorMessage(error)); }
    finally { if (identityEpoch === state.identityEpoch) { state.cancelling = false; updateSubmit(); schedulePoll(); } }
  }
  async function resumeRun() {
    if (!state.current?.resumable || state.submitting || state.needsAuth) return;
    if (!state.selected.size) { showError("Выберите источники для продолжения исследования."); setPanelCollapsed("context", false); return; }
    const epoch = state.identityEpoch, generation = state.generation, id = idOf(state.current);
    state.submitting = true; showError(""); updateSubmit();
    try {
      const run = await api("runs/" + encodeURIComponent(id) + "/resume", { method: "POST", body: { index_ids: [...state.selected] } });
      if (epoch !== state.identityEpoch) return;
      rememberRun(run);
      if (generation === state.generation) selectRun(run);
    } catch (error) { if (error.status !== 401 && epoch === state.identityEpoch) showError(errorMessage(error)); }
    finally { if (epoch === state.identityEpoch) { state.submitting = false; updateSubmit(); } }
  }
  async function loadWorkspace({ refreshModelState = true } = {}) {
    if (state.refreshController) state.refreshController.abort();
    const controller = new AbortController();
    state.refreshController = controller;
    connection("Подключаемся", "");
    try {
      const status = await api("status", { signal: controller.signal });
      if (controller.signal.aborted) return;
      state.status = status;
      state.needsAuth = false;
      showError("");
      renderStatus();
      renderHistory();
      loadConversations();
      if (state.current) refreshRun(idOf(state.current), state.generation);
      if (refreshModelState) refreshModel({ refreshPublic: false });
      loadSettings();
      loadMemory();
      loadSandboxProfiles();
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401) {
        showError(errorMessage(error));
        connection("Нет подключения", "error");
        $("sources-empty").textContent = "Не удалось загрузить источники.";
      }
    } finally { if (state.refreshController === controller) state.refreshController = null; }
  }

  $("new-run").addEventListener("click", resetWorkspace);
  $("history-more").addEventListener("click", () => loadConversations(true));
  $("refresh-conversation").addEventListener("click", () => loadConversation({ id: conversationState.id, title: conversationState.title }));
  $("conversation-more").addEventListener("click", () => loadConversation({ id: conversationState.id, title: conversationState.title }, true));
  $("delete-conversation").addEventListener("click", () => {
    const latest = conversationState.runs.at(-1);
    if (conversationState.id) openDeletion("conversation", { id: conversationState.id, title: conversationState.title, status: latest?.status, last_run_id: idOf(latest) });
  });
  $("settings-open").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", () => $("settings-dialog").close());
  $("settings-dialog").addEventListener("close", () => { clearSshEditor(); clearTlsEditor(); });
  $("settings-form").addEventListener("submit", (event) => { event.preventDefault(); saveSettings(); });
  $("settings-add-repository").addEventListener("click", () => addSettingResource("repository"));
  $("settings-add-connection").addEventListener("click", () => addSettingResource("connection"));
  $("settings-add-sandbox-profile").addEventListener("click", addSandboxProfile);
  $("settings-discard").addEventListener("click", () => {
    if (!settingsState.saved || settingsState.busy) return;
    settingsState.config = normalizedSettings(JSON.parse(settingsState.saved));
    settingsState.dirty = false;
    settingsState.probes.clear();
    settingsState.message = "Несохранённые изменения отменены.";
    settingsError();
    renderSettings();
  });
  document.querySelectorAll("[data-settings-tab]").forEach((tab) => {
    tab.addEventListener("click", () => selectSettingsTab(tab.dataset.settingsTab));
    tab.addEventListener("keydown", (event) => {
      const names = SETTINGS_TABS;
      const current = names.indexOf(tab.dataset.settingsTab);
      let next;
      if (event.key === "ArrowRight") next = (current + 1) % names.length;
      else if (event.key === "ArrowLeft") next = (current + names.length - 1) % names.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = names.length - 1;
      if (next !== undefined) { event.preventDefault(); selectSettingsTab(names[next], true); }
    });
  });
  $("ssh-new").addEventListener("click", () => openSshEditor());
  $("ssh-cancel").addEventListener("click", clearSshEditor);
  $("ssh-save").addEventListener("click", saveSshKey);
  $("ssh-file").addEventListener("change", async () => {
    const file = $("ssh-file").files?.[0], generation = sshState.generation;
    if (!file) return;
    $("ssh-private-key").value = "";
    if (file.size > 24000) { $("ssh-feedback").textContent = "Файл ключа должен быть не больше 24 КБ."; $("ssh-file").value = ""; return; }
    try {
      const text = await file.text();
      if (generation === sshState.generation && $("settings-dialog").open) { $("ssh-private-key").value = text; $("ssh-feedback").textContent = "Ключ прочитан из файла. Он будет отправлен только после сохранения."; }
    } catch (_) { if (generation === sshState.generation) $("ssh-feedback").textContent = "Не удалось прочитать файл ключа."; }
    if (generation === sshState.generation) $("ssh-file").value = "";
  });
  $("model-provider").addEventListener("change", updateModelConnection);
  $("model-profile-apply").addEventListener("click", applyProfileDefaults);
  $("model-profile").addEventListener("change", changeModelProfile);
  for (const id of ["direct-model-url", "direct-model-name", "direct-model-token", "direct-model-tls", "direct-model-timeout", "model-output-tokens"]) $(id).addEventListener("input", updateModelConnection);
  $("model-connection-save").addEventListener("click", () => changeModelConnection());
  $("model-connection-probe").addEventListener("click", () => changeModelConnection("simple"));
  $("model-autopilot-probe").addEventListener("click", () => changeModelConnection("autopilot"));
  $("tls-new").addEventListener("click", () => openTlsEditor());
  $("tls-cancel").addEventListener("click", clearTlsEditor);
  $("tls-save").addEventListener("click", saveTlsBundle);
  for (const [fileId, targetId, maxSize] of [["tls-ca-file", "tls-ca", 24000], ["tls-cert-file", "tls-cert", 24000], ["tls-key-file", "tls-key", 16000]]) {
    $(fileId).addEventListener("change", async () => {
      const file = $(fileId).files?.[0], generation = tlsState.generation;
      if (!file) return;
      $(targetId).value = "";
      if (file.size > maxSize) { $("tls-feedback").textContent = "Файл слишком большой. Максимум: " + Math.round(maxSize / 1000) + " КБ."; $(fileId).value = ""; return; }
      try { const text = await file.text(); if (generation === tlsState.generation && $("settings-dialog").open) { $(targetId).value = text; $("tls-feedback").textContent = "Файл прочитан. Он будет отправлен только после сохранения сертификатов."; } }
      catch (_) { if (generation === tlsState.generation) $("tls-feedback").textContent = "Не удалось прочитать файл."; }
      if (generation === tlsState.generation) $(fileId).value = "";
    });
  }
  $("discoveries-refresh").addEventListener("click", loadAdminDiscoveries);
  $("memory-open").addEventListener("click", openMemory);
  $("memory-close").addEventListener("click", () => $("memory-dialog").close());
  $("memory-dialog").addEventListener("close", clearMemoryEditor);
  $("memory-new").addEventListener("click", () => editMemory());
  $("memory-load-more").addEventListener("click", () => loadMemory(true));
  $("memory-cancel").addEventListener("click", clearMemoryEditor);
  $("memory-editor").addEventListener("submit", saveMemory);
  $("memory-enabled").addEventListener("change", changeMemoryPreference);
  $("feedback-helpful").addEventListener("click", () => saveRunFeedback("helpful"));
  $("feedback-unhelpful").addEventListener("click", () => saveRunFeedback("unhelpful"));
  $("feedback-toggle").addEventListener("click", () => expandFeedback($("feedback-comment-details").hidden));
  $("run-feedback-comment").addEventListener("input", () => renderRunFeedback(state.current));
  $("feedback-save-comment").addEventListener("click", () => {
    if (state.current?.feedback?.rating) saveRunFeedback(state.current.feedback.rating);
  });
  $("remember-run").addEventListener("click", () => { if (state.current) { openMemory(); editMemory(null, state.current); } });
  $("delete-run").addEventListener("click", () => { if (state.current) openDeletion("run", state.current); });
  $("delete-form").addEventListener("submit", confirmDeletion);
  $("delete-close").addEventListener("click", () => $("delete-dialog").close());
  $("delete-cancel").addEventListener("click", () => $("delete-dialog").close());
  $("delete-dialog").addEventListener("close", () => { deletionState.target = null; });
  enhanceDisclosure($("sources-disclosure"));
  enhanceDisclosure($("model-events"));
  $("question-form").addEventListener("submit", (event) => { event.preventDefault(); submitRun(); });
  $("question").addEventListener("input", updateCharacterCount);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing && (!event.shiftKey || event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (!$("submit-question").disabled) $("question-form").requestSubmit();
    }
  });
  document.querySelectorAll('input[name="mode"]').forEach((input) => input.addEventListener("change", () => {
    renderEffortDescription();
    renderExecutionEnvironment();
  }));
  $("effort").addEventListener("change", renderEffortDescription);
  $("sandbox-profile").addEventListener("change", () => { state.selectedSandboxProfile = $("sandbox-profile").value; renderExecutionEnvironment(); });
  document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => {
    $("question").value = button.dataset.question;
    updateCharacterCount();
    $("question").focus();
  }));
  $("select-all-sources").addEventListener("change", () => {
    const selected = $("select-all-sources").checked;
    state.selected = selected ? new Set(indexes().map((item) => item.id)) : new Set();
    $("source-list").querySelectorAll('input[type="checkbox"]').forEach((checkbox) => { checkbox.checked = selected; });
    updateSourceCount();
  });
  $("model-enable").addEventListener("click", () => changeModel("enable"));
  $("model-disable").addEventListener("click", () => changeModel("disable"));
  $("cancel-run").addEventListener("click", cancelRun);
  $("resume-run").addEventListener("click", resumeRun);
  $("deepen-run").addEventListener("click", () => { if (state.current) submitRun(state.current); });
  $("auth-open").addEventListener("click", openAuth);
  $("auth-close").addEventListener("click", () => $("auth-dialog").close());
  $("auth-dialog").addEventListener("close", () => { $("auth-secret").value = ""; });
  $("auth-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("auth-secret").value.trim();
    if (!value) return;
    state.auth = { kind: $("auth-kind").value, value };
    state.identityEpoch += 1;
    try { sessionStorage.setItem(AUTH_KEY, JSON.stringify(state.auth)); } catch (_) { /* In-memory for this page only. */ }
    $("auth-dialog").close();
    resetIdentity();
    loadWorkspace();
  });
  $("auth-clear").addEventListener("click", () => {
    state.auth = null;
    state.identityEpoch += 1;
    try { sessionStorage.removeItem(AUTH_KEY); } catch (_) { /* No stored credential to remove. */ }
    $("auth-dialog").close();
    resetIdentity();
    loadWorkspace();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { stopPolling(); stopModelPolling(); }
    else {
      const active = activeConversationRun();
      if (active) refreshRun(idOf(active), state.generation);
      refreshModel();
    }
  });
  window.addEventListener("pagehide", () => {
    stopPolling();
    stopModelPolling();
    if (state.modelActionController) state.modelActionController.abort();
    if (state.refreshController) state.refreshController.abort();
    conversationState.controller?.abort(); conversationState.listController?.abort();
    if (settingsState.controller) settingsState.controller.abort();
    for (const controller of [sshState.controller, tlsState.controller, modelConnectionState.controller, memoryState.controller, discoveryState.controller, discoveryState.adminController, deletionState.controller, feedbackState.controller, state.sandboxProfilesController]) controller?.abort();
    clearSshEditor(); clearTlsEditor();
  });
  initLayout();
  updateSubmit();
  renderHistory();
  loadWorkspace();
})();
