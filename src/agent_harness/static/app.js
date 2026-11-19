"use strict";

(() => {
  const AUTH_KEY = "agent-harness.auth.v1";
  const ACTIVE = new Set(["queued", "running", "cancelling"]);
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
    evidenceSignature: null, historySignature: null, answerSignature: null,
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
    connection("Требуется вход", "warning");
    showError("Для доступа к этому пространству войдите в портал или укажите выданный ключ.");
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
      const error = new Error("Требуется вход в рабочее пространство.");
      error.status = 401;
      throw error;
    }
    let result;
    try { result = await response.json(); }
    catch (_) { throw new Error("Не удалось прочитать ответ сервера. Попробуйте ещё раз."); }
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
  function indexes() { return Array.isArray(state.status?.indexes) ? state.status.indexes : []; }
  function maxSources() {
    const limit = Number(state.status?.limits?.max_indexes);
    return Number.isInteger(limit) && limit > 0 ? Math.min(16, limit) : 16;
  }
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
      state.selected = new Set(available.slice(0, maxSources()).map((item) => item.id));
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
        if (checkbox.checked && state.selected.size >= maxSources()) {
          checkbox.checked = false;
          showError("За один запрос можно выбрать не больше " + maxSources() + " источников.");
          return;
        }
        if (checkbox.checked) state.selected.add(item.id); else state.selected.delete(item.id);
        updateSourceCount();
      });
      label.append(checkbox, element("span", "", item.name || item.id));
      fragment.append(label);
    }
    $("source-list").replaceChildren(fragment);
    $("sources-empty").hidden = available.length > 0;
    $("sources-empty").textContent = "Доступные базы знаний пока не подключены.";
    $("select-all-sources").hidden = available.length === 0;
    updateSourceCount();
  }
  function updateSourceCount() {
    $("source-count").textContent = String(state.selected.size);
    $("select-all-sources").textContent = state.selected.size ? "Снять выбор" :
      (indexes().length > maxSources() ? "Выбрать первые " + maxSources() : "Выбрать все");
  }
  function renderStatus() {
    const status = state.status;
    const shared = status.identity_scope === "shared";
    $("identity-label").textContent = shared ? "Общее рабочее пространство" : "Личное рабочее пространство";
    $("history-title").textContent = shared ? "История пространства" : "Ваши запросы";
    $("history-list").setAttribute("aria-label", shared ? "История пространства" : "Личная история");
    $("history-empty").textContent = shared ?
      "Здесь появятся запросы этого пространства. К любому из них можно вернуться." :
      "Здесь появятся ваши исследования. К любому из них можно вернуться.";
    let notice = "";
    if (status.enabled === false) {
      notice = "Рабочее пространство сейчас выключено. Сохранённые запросы остаются в истории.";
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
    renderSources();
    updateSubmit();
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
  }
  function renderRun() {
    const run = state.current;
    const hasRun = Boolean(run);
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
    state.status = null;
    state.sourcesInitialized = false;
    state.runs = [];
    state.selected = new Set();
    state.submitting = false;
    state.cancelling = false;
    $("source-list").replaceChildren();
    $("source-count").textContent = "0";
    $("sources-empty").hidden = false;
    $("sources-empty").textContent = "Загружаем доступные источники…";
    $("select-all-sources").hidden = true;
    $("notice").hidden = true;
    $("portal-link").hidden = true;
    resetWorkspace();
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
      showError("Выберите хотя бы одну базу знаний в области поиска.");
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
  async function loadWorkspace() {
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
    } catch (error) {
      if (error.name !== "AbortError" && error.status !== 401) {
        showError(errorMessage(error));
        connection("Нет подключения", "error");
        $("sources-empty").textContent = "Не удалось загрузить источники.";
      }
    } finally { if (state.refreshController === controller) state.refreshController = null; }
  }

  $("new-run").addEventListener("click", resetWorkspace);
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
      "Уточнение связей и проверка вопроса по нескольким источникам." : "Короткий ответ по найденным источникам.";
  }));
  document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => {
    $("question").value = button.dataset.question;
    updateCharacterCount();
    $("question").focus();
  }));
  $("select-all-sources").addEventListener("click", () => {
    state.selected = state.selected.size ? new Set() : new Set(indexes().slice(0, maxSources()).map((item) => item.id));
    renderSources();
  });
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
    if (document.hidden) stopPolling();
    else if (state.current && ACTIVE.has(state.current.status)) refreshRun(idOf(state.current), state.generation);
  });
  window.addEventListener("pagehide", () => {
    stopPolling();
    if (state.refreshController) state.refreshController.abort();
  });
  updateSubmit();
  renderHistory();
  loadWorkspace();
})();
