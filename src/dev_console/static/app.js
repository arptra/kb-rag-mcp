"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const tokenKey = "dev-console-token";
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (fragment.has("token")) {
    sessionStorage.setItem(tokenKey, fragment.get("token"));
    history.replaceState(null, "", location.pathname + location.search);
  }
  const token = sessionStorage.getItem(tokenKey);
  // Reopening a new launch link in this same tab can be a hash-only navigation.
  window.addEventListener("hashchange", () => {
    if (new URLSearchParams(location.hash.slice(1)).has("token")) location.reload();
  });
  const state = { selected: null, detail: null, overview: null, busy: false, polling: false,
    timer: null, authenticated: true, tab: "events", plan: null, planPath: null,
    goalSession: null, eventSignature: null, noteSignature: null, doctor: null, round: null,
    sessionsSignature: null, planSignature: null, checksSignature: null, artifactsSignature: null,
    probing: false };
  let toastTimer;
  const statuses = {
    recording: "Запись", review_evidence: "Пакет на проверке", planning: "Диагностика",
    awaiting_plan_approval: "Утвердите план", editing: "Правки в GigaCode",
    awaiting_check_approval: "Разрешите проверки", verifying: "Проверки",
    awaiting_scenario_check: "Повторите сценарий", resolved: "Исправлено",
    needs_attention: "Нужно внимание", paused: "Пауза", scenario_unverified: "Сценарий не подтверждён",
    retry_pending: "Следующая попытка", queued: "В очереди", failed: "Ошибка", stopped: "Остановлено",
  };
  const kinds = { log: "Журнал сервера", process_output: "Вывод процесса", repair_phase: "Этап исправления",
    user_note: "Ваше наблюдение", skill_sync_failed: "Ошибка синхронизации", collector_error: "Ошибка сбора",
    process_started: "Запуск процесса", process_finished: "Процесс завершён", session_created: "Сессия создана",
    console_recording_started: "Запись включена", bundle_created: "Пакет диагностики", log_tail: "Хвост лога",
    log_rotated: "Ротация лога", log_gap: "Пропуск в логе", watch_stopped: "Запись остановлена" };

  function element(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  }
  function hidden(id, value) { $(id).classList.toggle("hidden", value); }
  function time(value, date = false) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "—";
    return date ? parsed.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
      : parsed.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function sessionUrl(id = state.selected) { return "/api/sessions/" + encodeURIComponent(id); }
  function message(error) { return error instanceof Error ? error.message : String(error); }
  function alertError(error) { $("alert").textContent = message(error); hidden("alert", false); }
  function toast(text) {
    clearTimeout(toastTimer); $("toast").textContent = text; hidden("toast", false);
    toastTimer = setTimeout(() => hidden("toast", true), 4500);
  }
  async function api(path, body) {
    const response = await fetch(path, { method: body === undefined ? "GET" : "POST",
      headers: { Authorization: "Bearer " + token, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), cache: "no-store", credentials: "omit" });
    let data;
    const content = await response.text();
    try { data = JSON.parse(content); } catch { data = content; }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        state.authenticated = false;
        throw new Error("Доступ к локальной консоли не подтверждён. Откройте полный адрес с токеном из терминала, в котором запущен dev-режим.");
      }
      const detail = typeof data === "object" ? (data.error || data.detail || data.message) : data;
      throw new Error(typeof detail === "string" ? detail : "Запрос не выполнен (" + response.status + ")");
    }
    return data;
  }
  function errorStatus(status) { return ["needs_attention", "failed"].includes(status); }
  function warningStatus(status) { return status?.startsWith("awaiting_") || ["paused", "scenario_unverified", "review_evidence"].includes(status); }
  function busyButtons() {
    const detail = state.detail;
    const running = Boolean(detail?.fix?.running);
    for (const id of ["new-session", "empty-create", "recording-toggle", "bundle-button", "note-button", "create-submit"]) {
      $(id).disabled = state.busy || !state.authenticated;
    }
    const noTerminal = state.overview?.interactive_available === false;
    const noCli = state.doctor?.available === false;
    $("fix-button").disabled = state.busy || state.probing || running || !state.authenticated || !$("goal").value.trim() || noCli || noTerminal;
    hidden("fix-unavailable", !noTerminal && !noCli);
    $("fix-unavailable").textContent = noTerminal ? "Для исправлений запустите dev-консоль в обычном интерактивном терминале. Сбор логов доступен и сейчас." : noCli ? "GigaCode пока не готов. Проверьте установку и поддерживаемые флаги в терминале запуска. Запись логов доступна." : "";
    hidden("doctor-retry", !noCli && !state.probing);
    $("doctor-retry").disabled = state.probing || !state.authenticated;
    $("doctor-retry").textContent = state.probing ? "Проверяем GigaCode…" : "Проверить GigaCode снова";
    $("fix-button").textContent = running ? "Цикл исправления выполняется…" : "Подготовить исправление ↗";
    for (const id of ["approve-button", "refuse-button"]) $(id).disabled = state.busy || !state.authenticated;
    $("cancel-button").disabled = state.busy || Boolean(detail?.fix?.cancellation_requested);
  }
  async function mutate(path, body, success) {
    if (state.busy) return null;
    state.busy = true; hidden("alert", true); busyButtons();
    try {
      const result = await api(path, body);
      if (success) toast(success);
      return result;
    } catch (error) { alertError(error); return null; }
    finally { state.busy = false; busyButtons(); schedulePoll(0); }
  }
  function renderSessions(sessions) {
    const signature = JSON.stringify([state.selected, sessions.map((item) => [item.id, item.label, item.goal, item.status, time(item.updated_at || item.created_at, true)])]);
    if (signature === state.sessionsSignature) return;
    state.sessionsSignature = signature;
    $("session-count").textContent = sessions.length;
    const list = $("sessions"); list.replaceChildren();
    if (!sessions.length) { list.append(element("p", "sidebar-empty", "Здесь появится история ваших тестов.")); return; }
    for (const item of sessions) {
      const button = element("button", "session-item" + (item.id === state.selected ? " selected" : ""));
      button.type = "button";
      button.setAttribute("aria-current", item.id === state.selected ? "page" : "false");
      button.append(element("strong", "", item.label || item.goal || "Сессия тестирования"));
      const metadata = element("div", "session-meta");
      const status = element("span");
      status.append(element("span", "dot" + (errorStatus(item.status) ? " error" : warningStatus(item.status) ? " warning" : "")),
        document.createTextNode(statuses[item.status] || item.status || "Сессия"));
      metadata.append(status, element("span", "", time(item.updated_at || item.created_at, true)));
      button.append(metadata);
      button.addEventListener("click", () => selectSession(item.id));
      list.append(button);
    }
  }
  function selectSession(id) {
    if (state.selected === id) return;
    state.selected = id; state.detail = null; state.plan = null; state.planPath = null;
    state.eventSignature = null; state.noteSignature = null; state.goalSession = null;
    state.planSignature = null; state.checksSignature = null; state.artifactsSignature = null;
    state.round = null;
    hidden("approval-card", true); hidden("artifact-viewer", true);
    if (state.overview) renderSessions(state.overview.sessions || []);
    schedulePoll(0);
  }
  function emptyPanel(container, title, text, symbol = "◎") {
    const box = element("div", "panel-empty");
    box.append(element("span", "", symbol), element("strong", "", title), element("p", "", text));
    container.replaceChildren(box);
    return box;
  }
  function renderEvents() {
    const events = state.detail?.events || [];
    const needle = $("log-search").value.trim().toLocaleLowerCase("ru");
    const errorsOnly = $("errors-only").checked;
    const signature = JSON.stringify([events, needle, errorsOnly]);
    if (signature === state.eventSignature) return;
    state.eventSignature = signature;
    $("event-count").textContent = events.length;
    const container = $("events");
    const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;
    container.replaceChildren();
    const filtered = events.filter((event) => {
      const text = String(event.message || "") + " " + String(event.kind || "");
      return (!needle || text.toLocaleLowerCase("ru").includes(needle)) && (!errorsOnly || /error|failed|fatal|exception|traceback|ошибк/i.test(text));
    });
    if (!filtered.length) {
      emptyPanel(container, needle || errorsOnly ? "Событий по фильтру нет" : "Ждём ваш тестовый сценарий",
        needle || errorsOnly ? "Измените поиск или отключите фильтр ошибок." : "Запись наблюдает за существующими логами. Воспроизведите проблему в дашборде или запустите backend с захватом вывода.");
    } else {
      for (const event of filtered) {
        const isError = /error|failed|fatal|exception|traceback|ошибк/i.test(String(event.kind) + " " + String(event.message));
        const row = element("div", "event" + (isError ? " error" : "") + (event.kind === "repair_phase" ? " phase" : ""));
        const content = element("div");
        content.append(element("span", "event-kind", kinds[event.kind] || event.kind || "Событие"),
          element("pre", "event-message", String(event.message || "")));
        row.append(element("time", "", time(event.at)), content);
        container.append(row);
      }
    }
    if (atBottom) container.scrollTop = container.scrollHeight;
  }
  function renderPlan(plan) {
    const signature = JSON.stringify(plan || null);
    if (signature === state.planSignature) return;
    state.planSignature = signature;
    const container = $("plan-content");
    if (!plan || typeof plan !== "object") {
      emptyPanel(container, "Сначала разберёмся в причине", "После запуска исправления здесь появится план GigaCode: шаги, файлы и критерий готовности.");
      return;
    }
    container.replaceChildren(element("p", "plan-summary", plan.summary || "План исправления"));
    for (const [key, label] of [["steps", "Шаги"], ["files", "Файлы, которые изменятся"], ["checks", "Проверки"], ["acceptance", "Критерий готовности"], ["risks", "Ограничения и риски"]]) {
      if (!plan[key]) continue;
      const section = element("section", "plan-section"); section.append(element("h3", "", label));
      if (Array.isArray(plan[key])) {
        if (key === "files") for (const file of plan[key]) section.append(element("code", "file-chip", file));
        else {
          const list = element(key === "steps" ? "ol" : "ul");
          for (const item of plan[key]) list.append(element("li", "", item));
          section.append(list);
        }
      } else section.append(element("p", "", plan[key]));
      container.append(section);
    }
  }
  function renderChecks(session) {
    const container = $("checks"); const checks = session.last_checks || [];
    const signature = JSON.stringify(checks);
    if (signature === state.checksSignature) return;
    state.checksSignature = signature;
    if (!checks.length) { emptyPanel(container, "Результатов пока нет", "После правок вы увидите команды и разрешите их запуск. Здесь будут результаты каждой проверки.", "✓"); return; }
    container.replaceChildren();
    for (const check of checks) {
      const recordingErrors = Array.isArray(check.recording_errors) ? check.recording_errors : [];
      const success = check.returncode === 0 && !check.timed_out && !check.cancelled && !recordingErrors.length;
      const row = element("div", "check-row" + (success ? "" : " failed"));
      const header = element("div", "check-header");
      header.append(element("strong", "", check.check || check.label || "Проверка"), element("span", "check-result", (success ? "✓ Пройдена" : check.cancelled ? "Отменена" : recordingErrors.length ? "Журнал неполный" : check.timed_out ? "Лимит времени" : "Ошибка · код " + check.returncode) + (check.duration_seconds !== undefined ? " · " + check.duration_seconds + " с" : "")));
      row.append(header);
      if (check.output_tail || recordingErrors.length) {
        const details = element("details");
        const output = [check.output_tail, recordingErrors.length ? "Проблемы записи журнала:\n" + recordingErrors.join("\n") : ""].filter(Boolean).join("\n\n");
        details.append(element("summary", "", "Посмотреть вывод и диагностику"), element("pre", "", output)); row.append(details);
      }
      container.append(row);
    }
  }
  function artifactPath(item) { return typeof item === "string" ? item : item.path; }
  function artifactUrl(path) { return sessionUrl() + "/artifacts/" + path.split("/").map(encodeURIComponent).join("/"); }
  async function readArtifact(path) {
    const data = await api(artifactUrl(path));
    if (data && typeof data === "object" && "content" in data) return String(data.content);
    return typeof data === "string" ? data : JSON.stringify(data, null, 2);
  }
  async function viewArtifact(path) {
    const selected = state.selected;
    try {
      const content = await readArtifact(path);
      if (selected !== state.selected) return;
      $("artifact-name").textContent = path; $("artifact-content").textContent = content;
      hidden("artifact-viewer", false); changeTab("artifacts");
      $("artifact-viewer").scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (error) { alertError(error); }
  }
  function renderArtifacts(artifacts) {
    const signature = JSON.stringify(artifacts);
    if (signature === state.artifactsSignature) return;
    state.artifactsSignature = signature;
    const container = $("artifacts");
    if (!artifacts.length) { emptyPanel(container, "Диагностика будет здесь", "Пакеты, планы, результаты проверок и сведения об изменениях сохраняются в каталоге сессии.", "▤"); return; }
    container.replaceChildren();
    for (const item of [...artifacts].reverse()) {
      const path = artifactPath(item); if (!path) continue;
      const button = element("button", "artifact-row");
      button.append(element("span", "", path), element("small", "", typeof item.size === "number" ? (item.size / 1024).toFixed(1) + " КБ ↗" : "Открыть ↗"));
      button.addEventListener("click", () => viewArtifact(path)); container.append(button);
    }
  }
  function commandText(commands) {
    return (commands || []).map((command) => {
      if (Array.isArray(command)) return String(command[0]) + ": " + (Array.isArray(command[1]) ? command[1].join(" ") : String(command[1]));
      return (command.check || command.name || "Проверка") + ": " + (Array.isArray(command.argv) ? command.argv.map((item) => /\s/.test(item) ? JSON.stringify(item) : item).join(" ") : "");
    }).join("\n\n");
  }
  function renderApproval(detail, session) {
    const pending = detail.pending_approval;
    hidden("approval-card", !pending);
    if (!pending) return;
    const kind = pending.kind || "confirmation";
    const titles = { plan: "Проверьте и утвердите план", checks: "Разрешить запуск проверок?", scenario: "Исправление подтвердилось?", continue: "Продолжить с новыми данными?", confirmation: "Проверить пакет перед анализом" };
    const approve = { plan: "Утвердить план", checks: "Запустить эти проверки", scenario: "Да, проблема исчезла", continue: "Продолжить цикл", confirmation: "Передать данные GigaCode" };
    $("approval-title").textContent = titles[kind] || "Нужно ваше подтверждение";
    $("approval-message").textContent = pending.message || "Подтвердите следующий шаг.";
    $("approve-button").textContent = approve[kind] || "Подтвердить";
    $("refuse-button").textContent = kind === "scenario" ? "Нет, проблема осталась" : "Отказать и остановиться";
    const plan = pending.plan || session.proposed_plan || state.plan;
    let evidence = "";
    if (kind === "plan" && plan) evidence = JSON.stringify(plan, null, 2);
    else if (kind === "checks") evidence = commandText(session.proposed_checks?.length ? session.proposed_checks : pending.commands);
    else if (["scenario", "continue"].includes(kind)) evidence = pending.acceptance || session.acceptance || plan?.acceptance || "";
    else {
      const bundles = (detail.artifacts || []).map(artifactPath).filter((path) => path?.startsWith("bundle-")).sort();
      if (bundles.length) evidence = "Пакет: " + bundles.at(-1) + "\nОткройте его на вкладке «Артефакты» и проверьте содержимое перед передачей.";
    }
    $("approval-evidence").textContent = evidence; hidden("approval-evidence", !evidence);
  }
  function renderNotes(notes) {
    const signature = JSON.stringify(notes);
    if (signature === state.noteSignature) return;
    state.noteSignature = signature;
    $("notes").replaceChildren();
    for (const note of notes.slice(-3).reverse()) {
      const item = element("div", "note-item");
      item.append(element("time", "", time(note.at, true)), element("p", "", note.text || "")); $("notes").append(item);
    }
  }
  function renderDetail(detail) {
    state.detail = detail;
    const session = detail.session || detail;
    if (state.round !== (session.round || 0)) {
      state.round = session.round || 0; state.plan = null; state.planPath = null;
    }
    hidden("empty-state", true); hidden("workspace", false);
    $("session-title").textContent = session.label || "Сессия тестирования";
    $("session-id").textContent = session.id || state.selected;
    $("backend-command").textContent = "./scripts/dev-debug.sh run --session " + state.selected + " -- ./scripts/start-backend.sh";
    $("session-subtitle").textContent = session.message || "Наблюдайте за тестом и подготовкой исправлений.";
    $("status-badge").textContent = statuses[session.status] || session.status || "Запись";
    $("status-badge").className = "badge" + (errorStatus(session.status) ? " error" : warningStatus(session.status) ? " warning" : "");
    const recording = Boolean(detail.recording?.running);
    $("recording-dot").className = "dot" + (recording ? "" : " muted");
    $("recording-text").textContent = recording ? "Идёт запись логов" : "Запись остановлена";
    $("recording-toggle").textContent = recording ? "Остановить запись" : "Начать запись";
    $("updated-at").textContent = session.last_collection?.at ? "Собрано в " + time(session.last_collection.at) : "Ожидаем события";
    if (state.goalSession !== state.selected) {
      $("goal").value = session.goal || session.label || session.notes?.at(-1)?.text || "";
      $("note").value = ""; state.goalSession = state.selected;
    }
    const steps = { recording: 0, review_evidence: 1, planning: 1, awaiting_plan_approval: 2,
      editing: 3, awaiting_check_approval: 4, verifying: 4, awaiting_scenario_check: 5, resolved: 5, scenario_unverified: 5, retry_pending: 1 };
    const activeStep = steps[session.status] ?? -1;
    [...$("timeline").children].forEach((item, index) => { item.classList.toggle("active", index === activeStep); item.classList.toggle("complete", index < activeStep || session.status === "resolved"); });
    const nativeActive = detail.fix?.running && ["planning", "editing"].includes(session.status);
    const cancellation = detail.fix?.cancellation_requested;
    hidden("terminal-banner", !nativeActive && !cancellation);
    $("terminal-title").textContent = cancellation ? "Остановка цикла запрошена" : "GigaCode открыт в исходном терминале";
    $("terminal-message").textContent = cancellation ? "Остановка произойдёт после текущего шага. Завершите GigaCode через /quit в терминале запуска; для немедленного прерывания нажмите там Ctrl+C. Сохранённые правки не откатываются." : "Переключитесь в терминал, где запущен dev-режим. Подтверждайте каждую запись там. После завершения введите /quit — управление вернётся в эту консоль.";
    hidden("cancel-button", !detail.fix?.running);
    const plan = detail.pending_approval?.plan || session.proposed_plan || state.plan;
    if (plan) state.plan = plan;
    renderPlan(plan); renderEvents(); renderChecks(session); renderArtifacts(detail.artifacts || []);
    renderNotes(session.notes || []); renderApproval(detail, session); busyButtons();
    maybeLoadPlan(detail);
  }
  async function maybeLoadPlan(detail) {
    if (detail.session?.proposed_plan || detail.pending_approval?.plan) return;
    const currentRound = detail.session?.round_dir;
    const paths = (detail.artifacts || []).map(artifactPath).filter((path) => /round-\d+\/plan\.json$/.test(path || "") && (!currentRound || path === currentRound + "/plan.json")).sort();
    const path = paths.at(-1);
    if (!path || state.planPath === path) return;
    const selected = state.selected; state.planPath = path;
    try {
      const raw = JSON.parse(await readArtifact(path));
      if (selected !== state.selected) return;
      state.plan = raw.plan || raw; renderPlan(state.plan);
    } catch { /* Incomplete planning artifacts are retried after the next state update. */ state.planPath = null; }
  }
  function changeTab(tab) {
    state.tab = tab;
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const selected = button.dataset.tab === tab;
      button.classList.toggle("active", selected); button.setAttribute("aria-selected", String(selected));
      hidden("view-" + button.dataset.tab, !selected);
    });
  }
  function schedulePoll(delay = 2000) {
    clearTimeout(state.timer);
    if (document.hidden || !state.authenticated) return;
    state.timer = setTimeout(poll, delay);
  }
  async function poll() {
    if (state.polling || document.hidden || !state.authenticated) { schedulePoll(); return; }
    state.polling = true;
    try {
      const overview = await api("/api/state"); state.overview = overview;
      const sessions = overview.sessions || [];
      if (!state.selected || !sessions.some((item) => item.id === state.selected)) state.selected = overview.active_session_id || sessions[0]?.id || null;
      renderSessions(sessions);
      if (state.selected) {
        const selected = state.selected; const detail = await api(sessionUrl(selected));
        if (selected === state.selected) renderDetail(detail);
      } else { hidden("workspace", true); hidden("empty-state", false); }
      $("connection-dot").className = "dot"; $("connection-text").textContent = "Подключено · " + time(new Date().toISOString());
    } catch (error) {
      $("connection-dot").className = "dot warning"; $("connection-text").textContent = "Нет связи с сервером";
      alertError(error);
    } finally { state.polling = false; busyButtons(); schedulePoll(); }
  }
  async function doctor() {
    if (state.probing) return;
    state.probing = true; busyButtons();
    try {
      const result = await api("/api/doctor"); state.doctor = result;
      $("cli-indicator").replaceChildren(element("span", "dot" + (result.available ? "" : " warning")),
        element("span", "", result.available ? "GigaCode готов к запуску" : "GigaCode требует настройки"));
      $("cli-indicator").title = result.available ? (result.version || "Нативный CLI доступен") : (result.error || "Проверьте GigaCode в терминале запуска");
      if (!result.available) toast("Запись доступна. Для исправлений настройте GigaCode в терминале запуска.");
      busyButtons();
    } catch (error) { alertError(error); }
    finally { state.probing = false; busyButtons(); }
  }

  function openCreate() {
    if (state.overview?.recording?.running) {
      alertError("Сначала остановите текущую запись. Одновременно записывается одна сессия; её артефакты останутся сохранены.");
      if (state.overview.recording.session_id) selectSession(state.overview.recording.session_id);
      return;
    }
    if (!$("create-dialog").open) $("create-dialog").showModal();
  }
  $("new-session").addEventListener("click", openCreate); $("empty-create").addEventListener("click", openCreate);
  for (const id of ["close-create", "cancel-create"]) $(id).addEventListener("click", () => $("create-dialog").close());
  $("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = await mutate("/api/sessions", { label: $("create-label").value.trim(), log_paths: $("create-logs").value.split("\n").map((path) => path.trim()).filter(Boolean) }, "Запись началась. Воспроизведите проблему.");
    if (!result) return;
    const id = result.session?.id || result.id || result.active_session_id;
    $("create-dialog").close(); $("create-form").reset();
    if (id) selectSession(id); else { state.selected = null; schedulePoll(0); }
  });
  $("recording-toggle").addEventListener("click", () => mutate(sessionUrl() + "/recording/" + (state.detail?.recording?.running ? "stop" : "start"), {}, state.detail?.recording?.running ? "Запись остановлена. Артефакты сохранены." : "Запись включена."));
  $("bundle-button").addEventListener("click", async () => { const result = await mutate(sessionUrl() + "/bundle", {}, "Пакет диагностики сохранён."); if (result) changeTab("artifacts"); });
  $("note-button").addEventListener("click", async () => {
    const text = $("note").value.trim(); if (!text) { $("note").focus(); return; }
    const result = await mutate(sessionUrl() + "/note", { text }, "Наблюдение добавлено в сессию.");
    if (result) $("note").value = "";
  });
  $("goal").addEventListener("input", busyButtons);
  $("doctor-retry").addEventListener("click", doctor);
  $("fix-button").addEventListener("click", async () => {
    const goal = $("goal").value.trim(); const max_rounds = Number($("max-rounds").value);
    if (!goal) { $("goal").focus(); return; }
    if (!Number.isInteger(max_rounds) || max_rounds < 1 || max_rounds > 10) { alertError("Лимит попыток должен быть целым числом от 1 до 10."); return; }
    await mutate(sessionUrl() + "/fix", { goal, max_rounds }, "Запущена подготовка исправления. Следите за запросами подтверждения.");
  });
  $("cancel-button").addEventListener("click", () => mutate(sessionUrl() + "/cancel", {}, "Остановка запрошена. Активный GigaCode завершите через /quit в терминале."));
  for (const [id, approved] of [["approve-button", true], ["refuse-button", false]]) $(id).addEventListener("click", async () => {
    const pending = state.detail?.pending_approval; if (!pending) return;
    const result = await mutate("/api/approvals/" + encodeURIComponent(pending.id), { approved, session_id: pending.session_id || state.selected });
    if (result && state.detail?.pending_approval?.id === pending.id) {
      hidden("approval-card", true); state.detail.pending_approval = null;
    }
  });
  $("log-search").addEventListener("input", renderEvents); $("errors-only").addEventListener("change", renderEvents);
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => changeTab(button.dataset.tab)));
  $("artifact-close").addEventListener("click", () => hidden("artifact-viewer", true));
  $("copy-backend").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("backend-command").textContent); toast("Команда скопирована. Запустите её в отдельном терминале."); }
    catch { toast("Выделите и скопируйте команду из блока выше."); }
  });
  document.addEventListener("visibilitychange", () => document.hidden ? clearTimeout(state.timer) : schedulePoll(0));
  window.addEventListener("pagehide", () => clearTimeout(state.timer));
  if (!token) {
    state.authenticated = false; hidden("empty-state", false);
    $("connection-text").textContent = "Требуется адрес из терминала"; $("connection-dot").className = "dot warning";
    alertError("Откройте полный адрес локальной dev-консоли из терминала запуска. Ссылка содержит токен доступа к этому процессу.");
    $("cli-indicator").replaceChildren(element("span", "dot muted"), element("span", "", "Ожидаем подключение")); busyButtons();
  } else { doctor(); schedulePoll(0); }
})();
