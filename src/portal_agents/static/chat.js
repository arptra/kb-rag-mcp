"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const question = $("question");
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
  async function request(path, options = {}) {
    const headers = new Headers(options.headers);
    if (options.method && options.method !== "GET") headers.set("Content-Type", "application/json");
    const password = storage.get("rag-admin-password");
    if (password) headers.set("X-KB-Admin-Password", password);
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    if (response.ok) return response;
    let message;
    if (response.status === 401) message = "Войдите в портал и обновите страницу.";
    else if (response.status === 403) message = "Для этого действия требуется дополнительный доступ.";
    else {
      const body = await response.text();
      message = body.length < 240 && !body.trim().startsWith("<") ? body : "";
    }
    const error = new Error(message || "Ошибка сервера (" + response.status + ").");
    error.status = response.status;
    throw error;
  }
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
      if (["tool_start", "tool_end", "model"].includes(item.event)) {
        const name = tools[item.text] || item.text;
        const status = item.event === "model" ? "Обращение к модели" :
          (item.event === "tool_start" ? "Выполняется · " : "Завершено · ") + name;
        timeline.summary.textContent = status;
        timeline.steps.append(element("li", "", status));
      } else if (item.event === "answer" || item.event === "error") {
        ended = true;
        timeline.details.hidden = !timeline.steps.childElementCount;
        timeline.summary.textContent = "Ход работы · " + timeline.steps.childElementCount;
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
    const headers = {};
    try {
      if (enable) {
        $("enable-model").disabled = true;
        const session = await fetch("/access/api/session", { credentials: "same-origin", cache: "no-store" });
        if (session.ok) {
          const payload = await session.json();
          if (payload.csrf_token) headers["X-CSRF-Token"] = payload.csrf_token;
        }
      }
      const response = await request("api/model", enable ? { method: "POST", headers, body: "{}" } : {});
      const info = await response.json();
      if (revision !== modelRequest) return;
      $("model-name").textContent = info.model;
      $("model-label").textContent = info.model;
      $("model-dot").dataset.state = info.state;
      $("model-state").textContent = modelStates[info.state] || info.state;
      $("model-panel").querySelector("summary").title = info.model + " · " + $("model-state").textContent;
      $("enable-model").hidden = !info.manager || !["disabled", "error"].includes(info.state);
      $("model-login").hidden = true;
      $("model-login").removeAttribute("href");
      if (info.manager && info.authentication_url) {
        const url = URL.parse ? URL.parse(info.authentication_url) : new URL(info.authentication_url);
        if (url && ["https:", "http:"].includes(url.protocol)) {
          $("model-login").href = url.href;
          $("model-login").hidden = false;
        }
      }
      $("portal-login").hidden = true;
      if (!["ready", "configured", "disabled", "error"].includes(info.state)) {
        pollTimer = setTimeout(() => modelStatus(), 2000);
      }
    } catch (error) {
      if (revision !== modelRequest) return;
      $("model-state").textContent = error.message;
      $("model-dot").dataset.state = "error";
      $("model-login").hidden = true;
      $("enable-model").hidden = true;
      $("portal-login").hidden = error.status !== 401;
      if (error.status === 401 || enable) notice(error.message);
    } finally {
      if (revision === modelRequest) $("enable-model").disabled = false;
    }
  }
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  question.addEventListener("input", resize);
  question.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
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
