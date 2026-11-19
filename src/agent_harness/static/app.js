"use strict";

(() => {
  const AUTH_KEY = "agent-harness.auth.v1";
  const ACTIVE = new Set(["queued", "running", "cancelling"]);
  const MODEL_PENDING = new Set(["starting", "authentication_required"]);
  const STATUS_LABELS = {
    queued: "Запрос принят", running: "Работаем над запросом", completed: "Готово",
    partial: "Собраны доступные сведения", cancelling: "Останавливаем работу", cancelled: "Остановлено", timed_out: "Время работы истекло",
    failed: "Не удалось завершить", interrupted: "Работа была прервана",
  };
  const PHASE_LABELS = {
    queued: "Ожидаем свободное место для запуска.", searching: "Ищем подтверждения в выбранных источниках.",
    investigating: "Проверяем связи и уточняем найденные сведения.",
    synthesizing: "Сопоставляем материалы и готовим ответ.", finished: "",
  };
  const $ = (id) => document.getElementById(id);
  const state = {
    status: null, runs: [], current: null, selected: new Set(), sourcesInitialized: false,
    submitting: false, cancelling: false, needsAuth: false, auth: null, generation: 0, identityEpoch: 0,
    pollTimer: null, pollController: null, refreshController: null, pollDelay: 1400,
    evidenceSignature: null, historySignature: null, answerSignature: null, presentedRunId: null,
    modelDetails: null, modelEndpointAvailable: null, modelError: "", modelBusy: null,
    modelPollTimer: null, modelPollController: null, modelActionController: null,
  };
  const settingsState = {
    canManage: false, checked: false, config: null, saved: "", dirty: false, busy: null,
    controller: null, generation: 0, activeTab: "repositories", message: "", probes: new Map(),
  };
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
  function modeLabel(value) { return value === "investigate" ? "Исследование" : "Быстрый ответ"; }
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
    renderModel();
    connection("Требуется вход", "warning");
    showError("Для входа в Harness войдите в портал или укажите ключ доступа к порталу.");
    openAuth();
    updateSubmit();
  }
  async function api(path, { method = "GET", body, signal } = {}) {
    const identityEpoch = state.identityEpoch;
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (state.auth) headers[state.auth.kind === "admin" ? "X-KB-Admin-Password" : "Authorization"] =
      state.auth.kind === "admin" ? state.auth.value : "Bearer " + state.auth.value;
    const response = await fetch(new URL("./api/" + path, document.baseURI), {
      method, headers, credentials: "same-origin", signal,
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    if (response.status === 401) {
      if (identityEpoch === state.identityEpoch) requireAuth();
      const error = new Error("Требуется вход в Harness.");
      error.status = 401;
      throw error;
    }
    let result;
    try { result = await response.json(); }
    catch (_) {
      const error = new Error("Не удалось прочитать ответ сервера. Попробуйте ещё раз.");
      error.status = response.status;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(typeof result.error === "string" ? result.error : "Не удалось выполнить запрос.");
      error.status = response.status;
      throw error;
    }
    return result;
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
        ...resource(item), ref: String(item.ref || "HEAD").trim(),
      })),
      connections: (Array.isArray(value.connections) ? value.connections : []).map((item) => ({
        ...resource(item), kind: item.kind === "a2a" ? "a2a" : "mcp", allowed_tools: strings(item.allowed_tools),
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
    $("settings-open").hidden = true;
    if ($("settings-dialog").open) $("settings-dialog").close();
    for (const id of ["settings-repositories", "settings-connections", "settings-sandbox"]) $(id).replaceChildren();
    settingsError();
    updateSettingsChrome();
  }
  function updateSettingsChrome() {
    const busy = Boolean(settingsState.busy);
    $("settings-open").hidden = !settingsState.canManage || state.needsAuth;
    $("settings-fields").disabled = busy || !settingsState.canManage;
    $("settings-save").disabled = busy || !settingsState.canManage || !settingsState.dirty;
    $("settings-discard").disabled = busy || !settingsState.dirty;
    $("settings-dialog").setAttribute("aria-busy", String(busy));
    $("settings-save").textContent = settingsState.busy === "saving" ? "Сохраняем…" : "Сохранить";
    $("settings-add-repository").disabled = (settingsState.config?.repositories.length || 0) >= 100;
    $("settings-add-connection").disabled = (settingsState.config?.connections.length || 0) >= 100;
    $("settings-fields").querySelectorAll("[data-probe]").forEach((button) => {
      button.disabled = busy || settingsState.dirty;
      button.title = settingsState.dirty ? "Сначала сохраните изменения" : "Проверить сохранённые настройки";
    });
    const busyText = { loading: "Загружаем настройки…", saving: "Сохраняем настройки…", probing: "Проверяем подключение…" };
    $("settings-feedback").textContent = busyText[settingsState.busy] || (settingsState.dirty ?
      "Есть изменения. Сохраните их перед проверкой подключений." : settingsState.message || "Настройки сохраняются на сервере.");
  }
  function markSettingsChanged() {
    settingsState.dirty = JSON.stringify(settingsState.config) !== settingsState.saved;
    settingsState.message = "";
    settingsError();
    updateSettingsChrome();
  }
  function selectSettingsTab(name, focus = false) {
    if (!["repositories", "connections", "sandbox"].includes(name)) return;
    settingsState.activeTab = name;
    for (const tab of document.querySelectorAll("[data-settings-tab]")) {
      const active = tab.dataset.settingsTab === name;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      $("settings-panel-" + tab.dataset.settingsTab).hidden = !active;
      if (active && focus) tab.focus();
    }
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
    const button = element("button", "secondary-button", kind === "sandbox" ? "Проверить песочницу" : "Проверить подключение");
    button.type = "button";
    button.dataset.probe = key;
    button.addEventListener("click", () => probeSettings(kind, entry?.id));
    actions.append(button);
    if (kind === "repository") actions.append(element("p", "settings-probe-note", "Проверим адрес и готовность песочницы. Репозиторий будет загружен при исследовании."));
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
    fields.append(settingField(repository ? "HTTPS-адрес репозитория" : "Адрес сервера", entry.url, (value) => { entry.url = value; }, {
      type: "url", required: true, maxLength: 2000, wide: true,
      help: "Адрес без токена, логина, пароля и параметров. Для передачи токена используйте переменную на сервере.",
    }).wrapper);
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
    selectSettingsTab(settingsState.activeTab);
    updateSettingsChrome();
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
      ...(repository ? { ref: "HEAD" } : { kind: "mcp", allowed_tools: [] }) });
    markSettingsChanged();
    renderSettings();
    const card = $("settings-" + collection).lastElementChild;
    card.querySelector('input[type="text"]').focus();
    appear(card);
  }
  async function saveSettings() {
    if (!settingsState.canManage || !settingsState.dirty || settingsState.busy || state.needsAuth) return;
    const invalid = $("settings-fields").querySelector(":invalid");
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
  function indexes() { return Array.isArray(state.status?.indexes) ? state.status.indexes : []; }
  function sourceName(id) {
    const match = indexes().find((item) => item.id === id);
    return match?.name || "Источник";
  }
  function updateSubmit() {
    const disabled = state.submitting || state.needsAuth || !state.status || state.status.enabled === false;
    $("submit-question").disabled = disabled;
    $("submit-question").firstElementChild.textContent = state.submitting ? "Отправляем…" : "Отправить";
    $("deepen-run").disabled = disabled;
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
    const shared = status.identity_scope === "shared";
    $("identity-label").textContent = shared ? "Общая история" : "Личная история";
    $("history-title").textContent = shared ? "Общая история" : "Личная история";
    $("history-list").setAttribute("aria-label", shared ? "Общая история" : "Личная история");
    $("history-empty").textContent = shared ?
      "Здесь появятся запросы пользователей с общим доступом. К любому из них можно вернуться." :
      "Здесь появятся ваши исследования. К любому из них можно вернуться.";
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
    const current = modelState();
    const canManage = state.modelDetails?.can_manage === true && !state.needsAuth;
    const labels = { disabled: "Выключен", starting: "Запускается", authentication_required: "Нужен вход", ready: "Готов", error: "Ошибка запуска" };
    const messages = {
      disabled: "Подключите GigaCode, чтобы получать ответы на основе найденных материалов.",
      starting: "Запускаем общий исполнитель. Состояние обновится автоматически.",
      authentication_required: canManage ? "Войдите в GigaCode, чтобы завершить подключение." : "Для подключения модели администратору нужно войти в GigaCode.",
      ready: "Модель готова к работе с вашими запросами.",
      error: "GigaCode не удалось запустить. Можно повторить подключение.",
    };
    $("model-state-badge").textContent = state.status ? labels[current] : "Проверяем";
    $("model-state-badge").className = "model-state-badge " + (current === "ready" ? "ready" : current === "error" ? "error" : MODEL_PENDING.has(current) ? "pending" : "");
    $("model-message").textContent = !state.status ? "Проверяем готовность модели." :
      typeof model.message === "string" && model.message ? model.message :
      typeof model.reason === "string" && model.reason ? model.reason : messages[current];
    $("model-manager-hint").hidden = !state.status || canManage || current === "ready" || state.modelEndpointAvailable === null;
    $("model-manager-hint").textContent = current === "authentication_required" ?
      "Попросите администратора завершить вход в GigaCode." : "Для включения GigaCode обратитесь к администратору.";
    $("model-error").textContent = state.modelError;
    $("model-error").hidden = !state.modelError;
    $("model-actions").hidden = !canManage || state.modelEndpointAvailable === false;
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
      link($("model-auth-link"), state.modelDetails.authentication_url));
    // Privileged links and launch events never come from the public model status.
    const events = canManage && Array.isArray(state.modelDetails.events) ? state.modelDetails.events.slice(-5) : [];
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
    if (state.status && details?.can_manage === true) {
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
        state.modelEndpointAvailable !== false && MODEL_PENDING.has(modelState())) {
      state.modelPollTimer = window.setTimeout(() => refreshModel(), 2000);
    }
  }
  async function refreshModel({ refreshPublic = true } = {}) {
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
    if (state.modelDetails?.can_manage !== true || state.modelBusy || state.needsAuth ||
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
    state.runs = [run, ...state.runs.filter((item) => idOf(item) !== id)]
      .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    renderHistory();
  }
  function renderHistory() {
    const signature = JSON.stringify([idOf(state.current), state.runs.map((run) =>
      [idOf(run), run.question, run.created_at, run.mode, run.status])]);
    if (signature === state.historySignature) return;
    state.historySignature = signature;
    const fragment = document.createDocumentFragment();
    for (const run of state.runs) {
      const button = element("button", "history-item");
      button.type = "button";
      button.setAttribute("aria-current", String(idOf(run) === idOf(state.current)));
      button.title = run.question || "Запрос";
      const meta = element("span", "history-item-meta");
      const kind = ACTIVE.has(run.status) ? "active" : run.status === "completed" ? "completed" :
        ["failed", "timed_out"].includes(run.status) ? "failed" : "";
      const dot = element("span", "history-state-dot " + kind);
      dot.setAttribute("aria-hidden", "true");
      meta.append(dot, document.createTextNode(dateLabel(run.created_at) + " · " + modeLabel(run.mode)));
      button.append(element("span", "history-item-title", run.question || "Запрос"), meta);
      button.addEventListener("click", () => selectRun(run));
      fragment.append(button);
    }
    $("history-list").replaceChildren(fragment);
    $("history-count").textContent = String(state.runs.length);
    $("history-empty").hidden = state.runs.length > 0;
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
  function inlineText(target, text) {
    const pattern = /(\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\(([^)\s]+)\))/g;
    let start = 0;
    for (const match of text.matchAll(pattern)) {
      target.append(document.createTextNode(text.slice(start, match.index)));
      if (match[2]) target.append(element("strong", "", match[2]));
      else if (match[3]) target.append(element("code", "", match[3]));
      else if (safeUrl(match[5])) {
        const anchor = element("a", "", match[4]);
        link(anchor, match[5]);
        target.append(anchor);
      } else target.append(document.createTextNode(match[0]));
      start = match.index + match[0].length;
    }
    target.append(document.createTextNode(text.slice(start)));
  }
  function renderAnswer(text) {
    if (text === state.answerSignature) return;
    state.answerSignature = text;
    const fragment = document.createDocumentFragment();
    let paragraph = [], list = null, code = null;
    const flush = () => {
      if (paragraph.length) {
        const node = element("p");
        inlineText(node, paragraph.join("\n"));
        fragment.append(node);
        paragraph = [];
      }
      list = null;
    };
    for (const line of text.split("\n")) {
      if (/^\s*```/.test(line)) {
        flush();
        if (code !== null) {
          const pre = element("pre");
          pre.append(element("code", "", code.join("\n")));
          fragment.append(pre);
          code = null;
        } else code = [];
        continue;
      }
      if (code !== null) { code.push(line); continue; }
      const heading = line.match(/^#{1,6}\s+(.+)/);
      const bullet = line.match(/^\s*(?:[-*+]\s+|\d+[.)]\s+)(.+)/);
      if (heading) {
        flush();
        const node = element("h3");
        inlineText(node, heading[1]);
        fragment.append(node);
      } else if (bullet) {
        if (paragraph.length) flush();
        const type = /^\s*\d/.test(line) ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== type) { list = element(type); fragment.append(list); }
        const item = element("li"); inlineText(item, bullet[1]); list.append(item);
      } else if (!line.trim()) flush();
      else { if (list) list = null; paragraph.push(line); }
    }
    flush();
    if (code !== null) { const pre = element("pre"); pre.append(element("code", "", code.join("\n"))); fragment.append(pre); }
    $("answer").replaceChildren(fragment);
    if (text) appear($("answer"));
  }
  function renderRun() {
    const run = state.current;
    const hasRun = Boolean(run);
    const newPresentation = hasRun && idOf(run) !== state.presentedRunId;
    state.presentedRunId = hasRun ? idOf(run) : null;
    $("welcome").hidden = hasRun;
    $("suggestions").hidden = hasRun;
    $("run-panel").hidden = !hasRun;
    $("page-label").textContent = hasRun ? modeLabel(run.mode) : "Новый запрос";
    if (!run) { renderEvidence([]); renderHistory(); return; }
    const active = ACTIVE.has(run.status);
    $("run-mode").textContent = modeLabel(run.mode) + " · Усилие: " + effortLabel(run.effort);
    $("run-date").textContent = dateLabel(run.created_at, true);
    $("run-question").textContent = run.question || "Запрос";
    $("parent-note").hidden = !run.parent_run_id;
    $("parent-note").textContent = "Продолжение предыдущего исследования с повторной проверкой источников.";
    const evidenceCount = Array.isArray(run.evidence) ? run.evidence.length : 0;
    const status = run.status === "partial" && !evidenceCount ? "Недостаточно данных" :
      STATUS_LABELS[run.status] || "Состояние запроса обновляется";
    $("run-status").textContent = status;
    const phase = run.status === "cancelling" ? "Ожидаем завершения текущего действия." :
      active ? (PHASE_LABELS[run.phase] || "Ожидаем обновления от сервера.") :
      run.status === "partial" ? "Ответ может быть неполным. Проверьте найденные материалы и замечания." :
      ["failed", "interrupted", "timed_out"].includes(run.status) ? "Найденные до остановки материалы сохранены." : "";
    $("run-phase").textContent = phase;
    $("run-phase").hidden = !phase;
    $("run-status-dot").className = "run-status-dot " + (active ? "active" : run.status === "completed" ? "completed" : ["failed", "timed_out"].includes(run.status) ? "failed" : "");
    const announcement = [status, phase, "Найдено подтверждений: " + evidenceCount].filter(Boolean).join(". ");
    if ($("progress-live").textContent !== announcement) $("progress-live").textContent = announcement;
    $("cancel-run").hidden = !active;
    $("deepen-run").hidden = active || ["failed", "interrupted"].includes(run.status);
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
    $("run-metrics").textContent = metrics.join(" · ");
    updateSubmit();
    renderHistory();
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
    if (!document.hidden && !state.needsAuth && state.current && ACTIVE.has(state.current.status)) {
      state.pollTimer = window.setTimeout(() => refreshRun(idOf(state.current), state.generation), delay);
    }
  }
  async function refreshRun(id, generation) {
    if (state.pollController || document.hidden) return;
    const controller = new AbortController();
    state.pollController = controller;
    try {
      const run = await api("runs/" + encodeURIComponent(id), { signal: controller.signal });
      if (generation !== state.generation || id !== idOf(state.current)) return;
      state.current = run;
      state.pollDelay = 1400;
      rememberRun(run);
      renderRun();
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401 && generation === state.generation) {
        showError(errorMessage(error));
        state.pollDelay = Math.min(10000, state.pollDelay * 2);
      }
    } finally {
      if (state.pollController === controller) state.pollController = null;
      if (generation === state.generation && !controller.signal.aborted) schedulePoll();
    }
  }
  function selectRun(run) {
    stopPolling();
    state.generation += 1;
    state.current = run;
    state.pollDelay = 1400;
    showError("");
    renderRun();
    refreshRun(idOf(run), state.generation);
  }
  function resetWorkspace() {
    stopPolling();
    state.generation += 1;
    state.current = null;
    showError("");
    $("question").value = "";
    updateCharacterCount();
    renderRun();
    $("question").focus();
  }
  function resetIdentity() {
    resetSettingsAccess();
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
    const question = parent ? parent.question : $("question").value.trim();
    if (!question) { $("question").focus(); return; }
    const selected = parent ? (Array.isArray(parent.index_ids) ? parent.index_ids : []) : [...state.selected];
    if (!parent && indexes().length && !selected.length) {
      showError("Выберите хотя бы один источник в области поиска.");
      const summary = $("sources-disclosure").querySelector("summary");
      if (summary.getAttribute("aria-expanded") !== "true") summary.click();
      $("source-list").querySelector("input")?.focus();
      return;
    }
    const payload = {
      question,
      mode: parent ? "investigate" : document.querySelector('input[name="mode"]:checked').value,
      effort: parent ? "high" : $("effort").value,
      index_ids: selected,
      ...(parent ? { parent_run_id: idOf(parent) } : {}),
    };
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
      $("question").value = "";
      updateCharacterCount();
    } catch (error) { if (error.status !== 401 && identityEpoch === state.identityEpoch) showError(errorMessage(error)); }
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
  async function loadWorkspace({ refreshModelState = true } = {}) {
    if (state.refreshController) state.refreshController.abort();
    const controller = new AbortController();
    state.refreshController = controller;
    connection("Подключаемся", "");
    try {
      const [status, history] = await Promise.all([
        api("status", { signal: controller.signal }), api("runs", { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      state.status = status;
      state.runs = Array.isArray(history.runs) ? history.runs : [];
      state.needsAuth = false;
      showError("");
      renderStatus();
      renderHistory();
      if (state.current) refreshRun(idOf(state.current), state.generation);
      if (refreshModelState) refreshModel({ refreshPublic: false });
      loadSettings();
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401) {
        showError(errorMessage(error));
        connection("Нет подключения", "error");
        $("sources-empty").textContent = "Не удалось загрузить источники.";
      }
    } finally { if (state.refreshController === controller) state.refreshController = null; }
  }

  $("new-run").addEventListener("click", resetWorkspace);
  $("settings-open").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", () => $("settings-dialog").close());
  $("settings-form").addEventListener("submit", (event) => { event.preventDefault(); saveSettings(); });
  $("settings-add-repository").addEventListener("click", () => addSettingResource("repository"));
  $("settings-add-connection").addEventListener("click", () => addSettingResource("connection"));
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
      const names = ["repositories", "connections", "sandbox"];
      const current = names.indexOf(tab.dataset.settingsTab);
      let next;
      if (event.key === "ArrowRight") next = (current + 1) % names.length;
      else if (event.key === "ArrowLeft") next = (current + names.length - 1) % names.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = names.length - 1;
      if (next !== undefined) { event.preventDefault(); selectSettingsTab(names[next], true); }
    });
  });
  enhanceDisclosure($("sources-disclosure"));
  enhanceDisclosure($("model-events"));
  $("question-form").addEventListener("submit", (event) => { event.preventDefault(); submitRun(); });
  $("question").addEventListener("input", updateCharacterCount);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (!$("submit-question").disabled) $("question-form").requestSubmit();
    }
  });
  document.querySelectorAll('input[name="mode"]').forEach((input) => input.addEventListener("change", () => {
    $("mode-description").textContent = input.value === "investigate" ?
      "Базы знаний, код, MCP и другие агенты." : "Базы знаний, без запуска песочниц.";
  }));
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
      if (state.current && ACTIVE.has(state.current.status)) refreshRun(idOf(state.current), state.generation);
      refreshModel();
    }
  });
  window.addEventListener("pagehide", () => {
    stopPolling();
    stopModelPolling();
    if (state.modelActionController) state.modelActionController.abort();
    if (state.refreshController) state.refreshController.abort();
    if (settingsState.controller) settingsState.controller.abort();
  });
  updateSubmit();
  renderHistory();
  loadWorkspace();
})();
