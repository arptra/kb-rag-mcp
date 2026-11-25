"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const question = $("question");
  const commandList = $("command-list");
  const commands = [
    { name: "/подключения", alias: "/connections", description: "Открыть подключения", admin: true,
      run: () => window.location.assign($("agent-connections").href) },
    { name: "/новый", alias: "/new", description: "Начать новый чат", run: () => switchChat(null) },
    { name: "/модель", alias: "/model", description: "Настройки модели", admin: true, run: openSettings },
  ];
  let matches = [];
  let highlighted = 0;
  let dismissedDraft = null;
  const scroller = $("scroll");
  const idPattern = /^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i;
  const modelStates = {
    configured: "Подключение API настроено",
    disabled: "GigaCode выключен",
    ready: "Готов к работе",
    starting: "Запускается",
    checking: "Проверяем подключение",
    authentication_required: "Ожидает авторизации",
    error: "Ошибка подключения",
  };
  const tools = {
    list_indexes: "Список индексов", search_index: "Поиск по индексу",
    read_document: "Чтение документа", search_service_graph: "Поиск по графу сервисов",
    list_environment_tools: "Доступные инструменты среды", call_environment_tool: "Вызов инструмента среды",
    run_in_workspace: "Запуск кода в контейнере", task: "Запуск подагента",
    list_artifact_sources: "Список подключённых репозиториев", fetch_git_snapshot: "Загрузка файлов из Git",
    fetch_maven_artifact: "Загрузка зависимости Gradle", run_gradle: "Запуск задач Gradle",
    write_todos: "Обновление плана", read_file: "Чтение рабочего файла", write_file: "Запись рабочего файла",
    edit_file: "Редактирование рабочего файла", glob: "Поиск файлов", grep: "Поиск в файлах",
    ls: "Список рабочих файлов",
  };
  const storage = {
    get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* Private browsing. */ } },
  };
  let chats;
  try {
    chats = JSON.parse(storage.get("portal-agents-chats") || "[]")
      .filter((item) => idPattern.test(item.id) && Number.isFinite(item.created));
  } catch { chats = []; }
  let chat = storage.get("portal-agents-current");
  if (!chats.some((entry) => entry.id === chat)) chat = null;
  let active = null;
  let historyRequest = null;
  let pollTimer = null;
  let modelRequest = 0;
  let settingsRequest = null;

  function newId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (n) => n.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }
  function notice(message = "") {
    $("notice").textContent = message;
    $("notice").hidden = !message;
  }
  const { request, adminHeaders, adminLoginUrl } = window.PortalApi;
  function saveChats() {
    storage.set("portal-agents-current", chat);
    storage.set("portal-agents-chats", JSON.stringify(chats));
    const options = chats.map((item, index) => {
      const date = new Date(item.created);
      const label = "Чат " + (index + 1) + " · " + date.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      return new Option(label, item.id, false, item.id === chat);
    });
    $("recent").replaceChildren(...options.reverse());
    $("recent-field").hidden = chats.length < 2;
  }
  function controls() {
    $("send").hidden = Boolean(active);
    $("stop").hidden = !active;
    $("send").disabled = Boolean(historyRequest) || !question.value.trim();
    $("delete-chat").disabled = Boolean(active || historyRequest);
    updateCommands();
  }
  function hideCommands() {
    matches = [];
    commandList.hidden = true;
    question.removeAttribute("aria-activedescendant");
  }
  function selectCommand(index) {
    highlighted = index;
    for (const [i, option] of Array.from(commandList.children).entries()) {
      option.setAttribute("aria-selected", String(i === index));
    }
    const option = commandList.children[index];
    question.setAttribute("aria-activedescendant", option.id);
    option.scrollIntoView({ block: "nearest" });
  }
  function commandMatches(command, prefix) {
    return command.name.startsWith(prefix) || command.alias.startsWith(prefix);
  }
  function updateCommands() {
    const prefix = question.value.toLowerCase();
    if (active || historyRequest || document.activeElement !== question ||
        dismissedDraft === question.value || !/^\/[\p{L}]*$/u.test(prefix)) {
      hideCommands();
      return;
    }
    const previous = matches[highlighted];
    matches = commands.filter(command => commandMatches(command, prefix) &&
      (!command.admin || !$("agent-connections").hidden));
    if (!matches.length) { hideCommands(); return; }
    commandList.replaceChildren(...matches.map(command => {
      const option = element("button", "command-option");
      option.type = "button";
      option.id = "command-" + command.alias.slice(1);
      option.setAttribute("role", "option");
      option.tabIndex = -1;
      option.title = command.alias;
      option.append(element("strong", "", command.name), element("small", "", command.description));
      option.addEventListener("mousedown", e => e.preventDefault());
      option.addEventListener("click", () => runCommand(command));
      return option;
    }));
    commandList.hidden = false;
    selectCommand(Math.max(0, matches.indexOf(previous)));
  }
  async function runCommand(command) {
    if (active || historyRequest) return;
    question.value = "";
    resize();
    notice();
    try {
      if (command.admin && !(await (await request("api/model")).json()).manager) {
        notice("Команду " + command.name + " может использовать администратор портала.");
        return;
      }
      await command.run();
    } catch (error) { notice(error.message); }
  }
  function resize() {
    question.style.height = "auto";
    question.style.height = question.scrollHeight + "px";
    controls();
  }
  function scrollDown() { scroller.scrollTop = scroller.scrollHeight; }
  function nearEnd() { return scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 90; }

  function message(role, text = "", html = "") {
    const row = element("article", "message " + role);
    row.append(element("h2", "role", role === "user" ? "Вы" : "Агент"));
    const body = element("div", "body");
    // HTML only comes from the same-origin server's Markdown renderer with raw HTML disabled.
    if (role === "assistant" && html) body.innerHTML = html;
    else body.textContent = text;
    row.append(body);
    $("messages").append(row);
    $("welcome").hidden = true;
    return { row, body };
  }
  function switchChat(next) {
    active?.abort();
    active = null;
    historyRequest?.abort();
    historyRequest = null;
    const existing = Boolean(next);
    chat = next || newId();
    if (!existing) chats.push({ id: chat, created: Date.now() });
    saveChats();
    question.value = "";
    resize();
    $("chat-menu").open = false;
    $("messages").replaceChildren();
    $("welcome").hidden = false;
    notice();
    if (existing) restoreHistory(chat);
    else question.focus();
  }
  async function restoreHistory(id) {
    const controller = new AbortController();
    historyRequest = controller;
    controls();
    try {
      const response = await request("api/chats/" + id, { signal: controller.signal });
      const data = await response.json();
      if (historyRequest !== controller) return;
      for (const item of data.messages || []) message(item.role, item.text, item.html);
      scrollDown();
    } catch (error) {
      if (!controller.signal.aborted && historyRequest === controller) notice(error.message);
    } finally {
      if (historyRequest === controller) { historyRequest = null; controls(); }
    }
  }

  function newActivity(turn) {
    const details = element("details", "trace");
    const summary = element("summary", "", "Отправляем сообщение…");
    summary.setAttribute("aria-live", "polite");
    const steps = element("ol");
    details.append(summary, steps);
    turn.row.insertBefore(details, turn.body);
    return { details, summary, steps };
  }
  async function consume(response, apply) {
    if (!response.body) throw new Error("Браузер не получил поток ответа.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const process = (final) => {
      const lines = buffer.split("\n");
      buffer = final ? "" : lines.pop();
      for (const line of lines) if (line.trim()) apply(JSON.parse(line));
    };
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        process(false);
      }
      buffer += decoder.decode();
      process(true);
    } finally { reader.releaseLock(); }
  }
  async function submit() {
    const text = question.value.trim();
    if (!text || active || historyRequest) return;
    const exact = commands.find(c => [c.name, c.alias].includes(text.toLowerCase()));
    const command = matches[highlighted] || exact;
    if (command) return runCommand(command);
    if (text === "/") {
      dismissedDraft = null;
      question.focus();
      updateCommands();
      return;
    }
    const controller = new AbortController();
    active = controller;
    notice();
    message("user", text);
    const turn = message("assistant");
    const timeline = newActivity(turn);
    let ended = false;
    question.value = "";
    resize();
    scrollDown();

    function update(item) {
      if (active !== controller) return;
      const stick = nearEnd();
      if (item.event === "start") timeline.summary.textContent = "Запрос принят";
      if (item.event === "waiting") return;
      if (["tool_start", "tool_end", "model", "model_retry"].includes(item.event)) {
        let status;
        if (item.event === "model_retry") status = item.text;
        else if (item.event === "model") status = item.text || "Обращение к модели";
        else {
          const name = tools[item.text] || item.text;
          status = (item.event === "tool_start" ? "Выполняется · " : "Завершено · ") + name;
        }
        timeline.summary.textContent = status;
        timeline.steps.append(element("li", "", status));
      } else if (item.event === "answer" || item.event === "error") {
        ended = true;
        timeline.details.hidden = !timeline.steps.childElementCount && !item.reference;
        timeline.summary.textContent = "Ход работы · " + timeline.steps.childElementCount;
        if (item.event === "error") {
          turn.row.querySelector(".role").textContent = "Статус запроса";
          if (item.reference) {
            timeline.details.append(element("p", "", "Номер ошибки для администратора · " + item.reference));
          }
        }
        if (item.event === "answer" && item.html) turn.body.innerHTML = item.html;
        else turn.body.textContent = item.text;
        turn.body.classList.toggle("error", item.event === "error");
      }
      if (stick) scrollDown();
    }
    try {
      const response = await request("api/chat", {
        method: "POST", body: JSON.stringify({ question: text, chat }), signal: controller.signal,
      });
      await consume(response, update);
      if (!ended) {
        if (controller.signal.aborted) update({ event: "error", text: "Вы остановили запрос." });
        else throw new Error("Связь прервалась до получения ответа.");
      }
    } catch (error) {
      if (active === controller && !ended) update({
        event: "error", text: controller.signal.aborted ? "Вы остановили запрос." : error.message,
      });
    } finally {
      if (active === controller) { active = null; controls(); question.focus(); }
    }
  }

  async function modelStatus(enable = false) {
    clearTimeout(pollTimer);
    const revision = ++modelRequest;
    try {
      if (enable) $("enable-model").disabled = true;
      const response = await request("api/model", enable ? { method: "POST", headers: await adminHeaders(), body: "{}" } : {});
      const info = await response.json();
      if (revision !== modelRequest) return;
      const protocol = info.adapter === "openai" ? "OpenAI API" : "gpt2giga";
      $("model-name").textContent = info.provider === "api" ? protocol + " · " + info.model : info.model;
      $("model-label").textContent = info.model;
      $("model-dot").dataset.state = info.state;
      $("model-state").textContent = modelStates[info.state] || info.state;
      $("model-panel").querySelector("summary").title = info.model + " · " + $("model-state").textContent;
      $("open-model-settings").hidden = !info.manager;
      $("agent-connections").hidden = !info.manager;
      updateCommands();
      $("model-readonly").hidden = Boolean(info.manager);
      $("enable-model").hidden = !info.manager || info.provider === "api" || !["disabled", "error"].includes(info.state);
      $("model-login").hidden = true;
      $("model-login").removeAttribute("href");
      if (info.manager && info.authentication_url) {
        const url = URL.parse ? URL.parse(info.authentication_url) : new URL(info.authentication_url);
        if (url && ["https:", "http:"].includes(url.protocol)) {
          $("model-login").href = url.href;
          $("model-login").hidden = false;
        }
      }
      $("portal-login").hidden = !!info.manager;
      if (!info.manager) $("portal-login").href = await adminLoginUrl();
      if (!["ready", "configured", "disabled", "error"].includes(info.state)) {
        pollTimer = setTimeout(() => modelStatus(), 2000);
      }
    } catch (error) {
      if (revision !== modelRequest) return;
      $("model-state").textContent = error.message;
      $("model-dot").dataset.state = "error";
      $("model-login").hidden = true;
      $("enable-model").hidden = true;
      $("open-model-settings").hidden = true;
      $("agent-connections").hidden = true;
      updateCommands();
      $("model-readonly").hidden = true;
      $("portal-login").hidden = error.status !== 401;
      if (error.status === 401) $("portal-login").href = await adminLoginUrl();
      if (error.status === 401 || enable) notice(error.message);
    } finally {
      if (revision === modelRequest) $("enable-model").disabled = false;
    }
  }
  const settingsDialog = $("model-settings");
  const settingsForm = $("settings-form");
  const files = ["ca", "cert", "key"];

  function settingsStatus(text, state = "") {
    $("settings-status").textContent = text;
    $("settings-status").dataset.state = state;
  }
  function selectProvider() {
    const selection = $("settings-provider").value;
    const api = selection !== "gigacode";
    const native = selection === "gpt2giga";
    $("api-fields").hidden = !api;
    $("api-fields").disabled = !api;
    $("settings-url-hint").textContent = native
      ? "Укажите базовый адрес корпоративного API GigaChat."
      : "Укажите базовый адрес API или полный адрес Chat Completions.";
  }
  function loadProfile(info) {
    settingsForm.reset();
    $("settings-provider").value = info.provider === "api" ? (info.adapter || "gpt2giga") : info.provider;
    $("settings-url").value = info.base_url || "";
    $("settings-model").value = info.model_name || "";
    $("settings-source").textContent = ({
      legacy: "Найдены настройки прежнего Harness.",
      environment: "Используются настройки из окружения портала.",
    })[info.source] || "";
    $("settings-source").hidden = !$("settings-source").textContent;
    $("settings-warning").textContent = info.warning || "";
    $("settings-warning").hidden = !info.warning;
    for (const kind of files) $("saved-" + kind).textContent = info["has_" + kind] ? "Сохранён" : "";
    $("saved-api-key").textContent = info.has_api_key ? "Сохранён" : "";
    $("replace-field").hidden = ![info.has_ca, info.has_cert, info.has_key, info.has_api_key].some(Boolean);
    selectProvider();
  }
  function disableSettings(disabled) {
    $("settings-fields").disabled = disabled;
    $("test-settings").disabled = disabled;
    $("save-settings").disabled = disabled;
  }
  async function openSettings() {
    $("model-panel").open = false;
    loadProfile({ provider: "gigacode" });
    settingsRequest = new AbortController();
    const { signal } = settingsRequest;
    disableSettings(true);
    settingsStatus("Загружаем подключение…");
    settingsDialog.showModal();
    try {
      const response = await request("api/model/settings", { signal });
      const info = await response.json();
      if (signal.aborted) return;
      loadProfile(info);
      disableSettings(false);
      settingsStatus("");
      $("settings-provider").focus();
    } catch (error) {
      if (!signal.aborted) settingsStatus(error.message, "error");
    }
  }
  async function draftSettings() {
    const selection = $("settings-provider").value;
    if (selection === "gigacode") return { provider: "gigacode" };
    const draft = { provider: "api", adapter: selection };
    draft.base_url = $("settings-url").value.trim();
    draft.model_name = $("settings-model").value.trim();
    const replace = $("replace-credentials").checked;
    for (const kind of files) {
      const file = $("settings-" + kind).files[0];
      if (file) {
        const limit = kind === "key" ? 32 : 64;
        if (!file.size || file.size > limit * 1024) throw new Error("Выберите непустой PEM-файл размером до " + limit + " КБ.");
        draft[kind + "_pem"] = await file.text();
      } else if (replace) draft[kind + "_pem"] = "";
    }
    const token = $("settings-api-key").value.trim();
    if (token || replace) draft.api_key = token;
    return draft;
  }
  async function submitSettings(test) {
    if ($("settings-fields").disabled || !settingsForm.reportValidity()) return;
    const { signal } = settingsRequest;
    disableSettings(true);
    settingsStatus(test ? "Отправляем короткий запрос модели…" : "Сохраняем подключение…");
    try {
      const draft = await draftSettings();
      const response = await request(test ? "api/model/test" : "api/model/settings", {
        method: test ? "POST" : "PUT", headers: await adminHeaders(), body: JSON.stringify(draft), signal,
      });
      if (signal.aborted) return;
      if (test) settingsStatus("Модель ответила на проверочный запрос.", "success");
      else {
        const info = await response.json();
        if (signal.aborted) return;
        loadProfile(info);
        settingsStatus("Сохранено. Новые запросы используют выбранную модель.", "success");
        modelStatus();
      }
    } catch (error) {
      if (!signal.aborted) settingsStatus(error.message, "error");
    } finally {
      if (!signal.aborted) disableSettings(false);
    }
  }
  $("open-model-settings").addEventListener("click", openSettings);
  $("close-settings").addEventListener("click", () => settingsDialog.close());
  settingsDialog.addEventListener("close", () => {
    settingsRequest?.abort();
    settingsForm.reset();
    $("model-panel").querySelector("summary").focus();
    modelStatus();
  });
  $("settings-provider").addEventListener("change", selectProvider);
  settingsForm.addEventListener("input", () => settingsStatus(""));
  settingsForm.addEventListener("submit", (e) => { e.preventDefault(); submitSettings(false); });
  $("test-settings").addEventListener("click", () => submitSettings(true));
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  question.addEventListener("input", () => { dismissedDraft = null; resize(); });
  question.addEventListener("focus", updateCommands);
  $("composer").addEventListener("focusout", e => {
    if (!$("composer").contains(e.relatedTarget)) hideCommands();
  });
  question.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    if (matches.length && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      const delta = e.key === "ArrowDown" ? 1 : -1;
      selectCommand((highlighted + delta + matches.length) % matches.length);
    } else if (matches.length && e.key === "Escape") {
      dismissedDraft = question.value;
      hideCommands();
    } else if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });
  for (const button of document.querySelectorAll("[data-question]")) {
    button.addEventListener("click", () => { question.value = button.dataset.question; resize(); question.focus(); });
  }
  $("stop").addEventListener("click", () => active?.abort());
  $("new-chat").addEventListener("click", () => switchChat(null));
  $("recent").addEventListener("change", (e) => switchChat(e.target.value));
  $("delete-chat").addEventListener("click", () => { $("chat-menu").open = false; $("confirm-delete").showModal(); });
  $("confirm-delete").addEventListener("close", async () => {
    if ($("confirm-delete").returnValue !== "delete") return;
    $("confirm-delete").returnValue = "";
    const deleting = chat;
    try {
      await request("api/chats/" + deleting, { method: "DELETE" });
      chats = chats.filter((entry) => entry.id !== deleting);
      if (chat === deleting) switchChat(chats.at(-1)?.id || null);
      else saveChats();
    } catch (error) { notice(error.message); }
  });
  $("enable-model").addEventListener("click", () => modelStatus(true));
  $("model-panel").addEventListener("toggle", () => { if ($("model-panel").open) modelStatus(); });
  document.addEventListener("click", (e) => {
    for (const panel of document.querySelectorAll(".popover[open]")) {
      if (!panel.contains(e.target)) panel.open = false;
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") for (const panel of document.querySelectorAll(".popover[open]")) panel.open = false;
  });
  window.addEventListener("pagehide", () => { active?.abort(); historyRequest?.abort(); clearTimeout(pollTimer); });
  switchChat(chat);
  modelStatus();
})();
