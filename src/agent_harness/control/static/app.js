"use strict";
(() => {
  const $ = (id) => document.getElementById(id), AUTH_KEY = "agent-harness.auth.v1";
  const ACTIVE = new Set(["queued", "running", "cancelling"]);
  const MODES = { quick: "Быстро", investigate: "Исследовать", autonomous: "Автопилот" };
  const STATUSES = { queued: "В очереди", running: "В работе", cancelling: "Останавливается", completed: "Готово", partial: "С ограничениями", cancelled: "Остановлен", timed_out: "Истекло время", failed: "Ошибка", interrupted: "Прерван" };
  const STOP_REASONS = { completed: "Ответ завершён", time_limit: "Исчерпано время", cancelled: "Остановлено", failed: "Ошибка выполнения", interrupted: "Прервано перезапуском", budget_ceiling: "Потолок бюджета", context_limit: "Ограничение контекста", no_new_evidence: "Нет новых материалов", partial: "Ответ с ограничениями", active: "Выполняется" };
  const LABELS = {
    model_calls: "Основные вызовы модели", summary_calls: "Вызовы для сжатия беседы", source_calls: "Повторные обращения к источникам", tool_calls: "Вызовы инструментов", parallelism: "Параллельные обращения", document_reads: "Чтение документов", evidence_per_index: "Материалы из одного индекса", evidence_chars: "Размер выдержки, символы", document_chars: "Размер документа, символы", timeout_seconds: "Время работы, секунды", context_tokens: "Входной контекст, токены ≈",
    external_tool_timeout_seconds: "Ожидание внешнего инструмента, секунды", max_decision_errors: "Ошибки решения модели до остановки",
    enabled: "Включено", max_extensions: "Разрешённые расширения бюджета", minimum_progress: "Требовать продвижения исследования", window_tokens: "Окно модели, токены", response_reserve_tokens: "Резерв под ответ, токены", history_fraction: "Доля контекста для беседы", trigger_fraction: "Порог сжатия истории", keep_fraction: "Доля последних сообщений", summary_output_tokens: "Размер сводки, токены ≈", chars_per_token: "Символов на токен, оценка", summary_input_fraction: "Доля окна для подготовки сводки",
    quick_workers: "Одновременные быстрые запросы", investigate_workers: "Одновременные исследования", queue_max: "Размер каждой очереди", authentication_timeout_seconds: "Ожидание авторизации, секунды", cleanup_timeout_seconds: "Очистка ресурсов, секунды", source_timeout_seconds: "Ожидание источника, секунды", final_reserve_seconds: "Резерв времени для ответа, секунды", max_indexes: "Выбранные источники", results_per_index: "Общий предел материалов на индекс", max_evidence: "Материалы на запрос", answer_chars: "Размер показанного ответа, символы", retention_days: "Хранение истории, дни", max_runs_per_owner: "Запросы в истории пользователя", max_total_runs: "Запросы в общей истории", graph_recursion_limit: "Шаги графа на запуск", max_stalled_rounds: "Раунды без новых данных", max_answer_retries: "Повторы подготовки ответа", max_resume_attempts: "Продолжения прерванного запроса", memory_max_records: "Записи общей памяти", memory_retrieval_count: "Записи памяти на один ответ", memory_context_tokens: "Контекст общей памяти, токены ≈",
  };
  const HELP = {
    history_fraction: "От 0 до 0,8. Например, 0,35 означает 35% входного контекста.",
    trigger_fraction: "Запуск сжатия при заполнении этой доли бюджета истории. Например, 0,8 = 80%.",
    keep_fraction: "Последние сообщения остаются без пересказа. Значение должно быть ниже порога сжатия.",
    summary_input_fraction: "Запас окна модели для запроса, который составляет сводку.",
    max_extensions: "Сколько раз Автопилот может получить дополнительный бюджет в одном запросе.",
    minimum_progress: "Дополнительные ресурсы выдаются, когда исследование получает новые материалы.",
    context_tokens: "Оценка размера входа. Реальное окно модели и резерв ответа остаются дополнительными ограничениями.",
  };
  const state = { settings: null, schema: null, saved: "", revision: 0, activation: {}, auth: null, epoch: 0, busy: false, loading: false, charts: new Map(), timer: null, requests: new Set(), cancelling: new Set() };
  try { const value = JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null"); if (value && ["admin", "bearer"].includes(value.kind) && typeof value.value === "string") state.auth = value; } catch (_) {}
  const clone = (value) => JSON.parse(JSON.stringify(value));
  function node(tag, text, className) { const result = document.createElement(tag); if (text !== undefined) result.textContent = text; if (className) result.className = className; return result; }
  function number(value, digits = 0) { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("ru-RU", { maximumFractionDigits: digits }) : "—"; }
  function date(value) { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(parsed); }
  function notice(text, error = false) { const target = $(error ? "error" : "message"); target.textContent = text || ""; target.hidden = !text; }
  function dirty() { return state.settings && JSON.stringify(state.settings) !== state.saved; }
  function updateButtons() { $("save").disabled = state.busy || !dirty(); $("discard").disabled = state.busy || !dirty(); $("save").textContent = state.busy ? "Сохраняем…" : "Сохранить"; $("save-state").textContent = dirty() ? "Есть несохранённые изменения" : "Изменений нет"; $("settings-form").querySelectorAll("input,select").forEach((input) => { input.disabled = state.busy; }); }
  function resetAccess() { state.epoch += 1; for (const controller of state.requests) controller.abort(); state.requests.clear(); window.clearTimeout(state.timer); state.settings = null; state.saved = ""; state.busy = false; state.loading = false; for (const chart of state.charts.values()) chart.destroy(); state.charts.clear(); $("workspace").hidden = true; $("access-panel").hidden = false; $("connection").textContent = "Нужен доступ"; for (const id of ["summary", "active-runs", "recent-runs", "series-table", "stop-reasons", "queues", "autonomy-fields", "profile-fields", "ceiling-fields", "context-fields", "runtime-fields"]) $(id).replaceChildren(); }
  async function api(path, { method = "GET", body } = {}) {
    const epoch = state.epoch, controller = new AbortController(); state.requests.add(controller);
    const timer = window.setTimeout(() => controller.abort(), 25000);
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (state.auth) headers[state.auth.kind === "admin" ? "X-KB-Admin-Password" : "Authorization"] = state.auth.kind === "admin" ? state.auth.value : "Bearer " + state.auth.value;
    try {
      const response = await fetch(new URL("../api/control/" + path, document.baseURI), { method, headers, credentials: "same-origin", signal: controller.signal, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
      if ([401, 403].includes(response.status)) { if (epoch === state.epoch) resetAccess(); const error = new Error("Нужен доступ администратора Harness."); error.status = response.status; throw error; }
      let payload; try { payload = await response.json(); } catch (_) { throw new Error("Не удалось прочитать ответ сервера."); }
      if (!response.ok) { const error = new Error(payload.error || "Запрос не выполнен."); error.status = response.status; throw error; }
      return payload;
    } catch (error) { if (error.name === "AbortError" && epoch === state.epoch) throw new Error(method === "GET" ? "Сервер не ответил вовремя. Повторите обновление." : "Подтверждение не получено. Действие могло выполниться: обновите данные перед повтором."); throw error; }
    finally { window.clearTimeout(timer); state.requests.delete(controller); }
  }
  function fieldset(id, path, typeName, omit = []) {
    const target = $(id), values = path.reduce((value, key) => value[key], state.settings), schema = state.schema?.$defs?.[typeName]?.properties || {};
    const fragment = document.createDocumentFragment();
    for (const [key, value] of Object.entries(values)) {
      if (omit.includes(key) || (typeof value !== "number" && typeof value !== "boolean")) continue;
      const definition = schema[key] || {}, label = node("label", undefined, "field" + (typeof value === "boolean" ? " checkbox-field" : "")), input = node("input");
      label.append(node("span", LABELS[key] || definition.description || key, "field-label")); input.dataset.path = [...path, key].join(".");
      input.type = typeof value === "boolean" ? "checkbox" : "number";
      if (input.type === "checkbox") input.checked = value;
      else { input.value = value; input.required = true; input.step = definition.type === "integer" ? "1" : "any"; if (definition.minimum !== undefined) input.min = definition.minimum; if (definition.maximum !== undefined) input.max = definition.maximum; }
      input.addEventListener("input", () => { const next = input.type === "checkbox" ? input.checked : Number(input.value); if (input.type !== "checkbox" && (!input.value || !Number.isFinite(next))) { input.setCustomValidity("Укажите число."); return; } input.setCustomValidity(definition.exclusiveMinimum !== undefined && next <= definition.exclusiveMinimum ? "Значение должно быть больше " + definition.exclusiveMinimum : ""); values[key] = next; updateButtons(); });
      label.append(input);
      const help = HELP[key] || definition.description; if (help) label.append(node("small", help));
      if (path[0] === "runtime" && (state.activation.restart_fields || []).includes(key)) label.append(node("small", "Вступает в силу после перезапуска сервиса", "restart"));
      fragment.append(label);
    }
    const legends = { "autonomy-fields": "Самостоятельность", "profile-fields": "Начальный бюджет", "ceiling-fields": "Максимальные бюджеты Автопилота", "context-fields": "Автоматическое сжатие", "runtime-fields": "Ограничения сервиса" };
    target.replaceChildren(node("legend", legends[id] || "Настройки"), fragment);
  }
  function renderProfile() { if (!state.settings) return; const mode = $("profile-mode").value, effort = $("profile-effort").value; fieldset("profile-fields", ["profiles", mode, effort], "RunLimits"); $("profile-help").textContent = mode === "autonomous" ? "Стартовый бюджет. Агент может запросить расширение до потолков ниже." : mode === "quick" ? "Фиксированный бюджет. Дополнительные инструменты и поиски в этом режиме не запускаются." : "Фиксированный бюджет исследования без самостоятельного расширения."; }
  function applySettings(data) {
    state.settings = clone(data.settings); state.schema = data.schema; state.revision = data.revision; state.saved = JSON.stringify(state.settings); state.activation = data.activation || {};
    $("revision").textContent = "Редакция " + state.revision;
    fieldset("autonomy-fields", ["autonomy"], "AutonomySettings", ["caps"]); renderProfile(); fieldset("ceiling-fields", ["autonomy", "caps"], "RunLimits"); fieldset("context-fields", ["context"], "ContextSettings"); fieldset("runtime-fields", ["runtime"], "RuntimeLimits");
    const restart = state.activation.restart_required || [];
    $("activation-note").textContent = (state.activation.new_runs || "Настройки применяются к новым запросам.") + (restart.length ? ". Нужен перезапуск для: " + restart.map((key) => LABELS[key] || key).join(", ") + "." : ". Запущенные запросы сохраняют свои бюджеты.");
    $("external-limits").replaceChildren(...(data.external_limits || []).map((item) => node("p", item.message)));
    $("workspace").hidden = false; $("access-panel").hidden = true; $("connection").textContent = "Подключено"; updateButtons();
  }
  async function loadSettings() { const epoch = state.epoch; try { const data = await api("settings"); if (epoch !== state.epoch) return; applySettings(data); await refreshMetrics(); } catch (error) { if (error.name !== "AbortError") notice(error.status === 404 ? "Центр управления отключён в настройках сервиса." : error.message, true); } }
  async function saveSettings(event) { event?.preventDefault(); if (!state.settings || state.busy || !dirty()) return; const epoch = state.epoch, body = { revision: state.revision, settings: clone(state.settings) }; state.busy = true; updateButtons(); notice("", true); try { const data = await api("settings", { method: "PUT", body }); if (epoch !== state.epoch) return; applySettings(data); notice("Настройки сохранены. Новые запросы используют эту редакцию."); } catch (error) { if (epoch === state.epoch) notice(error.status === 409 ? "Настройки изменились в другой сессии. Ваши правки сохранены на странице. Обновите настройки перед повторным сохранением." : error.message, true); } finally { if (epoch === state.epoch) { state.busy = false; updateButtons(); } } }
  function table(headers, rows) { const result = node("table"), head = node("thead"), heading = node("tr"), body = node("tbody"); for (const value of headers) heading.append(node("th", value)); head.append(heading); for (const values of rows) { const row = node("tr"); for (const value of values) { const cell = node("td"); cell.append(typeof value === "object" && value !== null ? value : node("span", String(value))); row.append(cell); } body.append(row); } result.append(head, body); return result; }
  function renderRuns(id, runs, active = false) {
    if (!runs.length) { $(id).replaceChildren(node("p", active ? "Сейчас нет активных запросов." : "За выбранный период запросов пока нет.", "help")); return; }
    const rows = runs.map((run) => {
      const identity = node("div", String(run.id || "").slice(0, 10), "run-id"), details = node("details", undefined, "counter-details"); details.append(node("summary", "Все счётчики"));
      details.append(node("p", "Основные вызовы: " + number(run.decision_calls) + "; сжатие: " + number(run.summary_calls) + "; инструменты: " + number(run.tool_calls) + "; источники: " + number(run.source_calls) + "; расширения: " + number(run.extensions) + "; сжатия: " + number(run.compactions) + "; вход ≈ " + number(run.input_tokens_estimated) + "; выход ≈ " + number(run.output_tokens_estimated) + "; вход от API: " + number(run.input_tokens_reported) + "; выход от API: " + number(run.output_tokens_reported))); identity.append(details);
      const action = node("span", STOP_REASONS[run.stop_reason] || "");
      if (active && ACTIVE.has(run.status)) { action.textContent = ""; const button = node("button", run.status === "cancelling" ? "Останавливается" : "Остановить", "quiet"); button.type = "button"; button.disabled = run.status === "cancelling" || state.cancelling.has(run.id); button.addEventListener("click", () => cancelRun(run.id)); action.append(button); }
      return [identity, date(run.created_at), MODES[run.mode] || run.mode, STATUSES[run.status] || run.status, number(run.duration_ms / 1000, 1), number(run.model_calls), action];
    });
    $(id).replaceChildren(table(["Запрос", "Начало", "Режим", "Состояние", "Время, с", "Вызовы модели", active ? "Действие" : "Причина"], rows));
  }
  async function cancelRun(id) { if (state.cancelling.has(id)) return; const epoch = state.epoch; state.cancelling.add(id); try { await api("runs/" + encodeURIComponent(id) + "/cancel", { method: "POST", body: {} }); if (epoch === state.epoch) { notice("Запрошена остановка работы."); await refreshMetrics(); } } catch (error) { if (epoch === state.epoch) notice(error.message, true); } finally { state.cancelling.delete(id); } }
  function chart(id, labels, datasets) { if (!window.Chart) return; state.charts.get(id)?.destroy(); const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches; state.charts.set(id, new window.Chart($(id), { type: "line", data: { labels, datasets: datasets.map((item, index) => ({ ...item, borderColor: ["#9ccfff", "#d0d0d8", "#e9bf7b"][index], backgroundColor: "transparent", borderWidth: 2, pointRadius: labels.length > 30 ? 0 : 2, tension: .15 })) }, options: { responsive: true, maintainAspectRatio: false, animation: reduced ? false : { duration: 250 }, interaction: { intersect: false, mode: "index" }, plugins: { legend: { labels: { color: "#c4c4cc", font: { size: 12 }, usePointStyle: true } } }, scales: { x: { ticks: { color: "#a0a0aa", maxTicksLimit: 8 }, grid: { color: "#ffffff08" } }, y: { beginAtZero: true, ticks: { color: "#a0a0aa", precision: 0 }, grid: { color: "#ffffff0d" } } } } })); }
  function renderMetrics(data) {
    const s = data.summary || {}, series = data.series || [], labels = series.map((item) => String(item.date || "").slice(5));
    $("summary").replaceChildren(...[["Запросы", number(s.runs), "За выбранный период"], ["Активные работы", number(s.active_now), "Во всех очередях сейчас"], ["Вызовы модели", number(s.model_calls), "Включая " + number(s.summary_calls) + " для сжатия"], ["Среднее время", number(s.duration_mean_ms / 1000, 1) + " с", "Завершённые запросы"]].map(([label, value, hint]) => { const item = node("article", undefined, "kpi"); item.append(node("span", label), node("strong", value), node("span", hint)); return item; }));
    $("metrics-note").textContent = "Данные сохранённых запусков; дни по UTC. Токены: оценка входа ≈ " + number(s.input_tokens_estimated) + ", фактически сообщено API: " + number(s.input_tokens_reported) + " (запросов с данными API: " + number(s.reported_usage_runs) + "). Стоимость не рассчитывается.";
    chart("runs-chart", labels, [{ label: "Всего", data: series.map((x) => x.runs) }, { label: "Готово", data: series.map((x) => x.completed) }, { label: "Ошибки", data: series.map((x) => x.failed) }]);
    chart("calls-chart", labels, [{ label: "Модель", data: series.map((x) => x.model_calls) }, { label: "Источники", data: series.map((x) => x.source_calls) }]);
    chart("duration-chart", labels, [{ label: "Среднее", data: series.map((x) => x.duration_mean_ms / 1000) }, { label: "95-й процентиль", data: series.map((x) => x.duration_p95_ms / 1000) }]);
    chart("adaptive-chart", labels, [{ label: "Расширения", data: series.map((x) => x.extensions) }, { label: "Сжатия", data: series.map((x) => x.compactions) }]);
    $("series-table").replaceChildren(table(["Дата UTC", "Запросы", "Готово", "Ошибки", "Модель", "Источники", "Среднее, с", "Расширения", "Сжатия"], series.map((x) => [x.date, x.runs, x.completed, x.failed, x.model_calls, x.source_calls, number(x.duration_mean_ms / 1000, 1), x.extensions, x.compactions])));
    const listRow = (label, value) => { const row = node("div", undefined, "row-list"); row.append(node("span", label), node("strong", value)); return row; };
    $("stop-reasons").replaceChildren(...(data.stop_reasons || []).map((item) => listRow(item.label || item.reason, number(item.count))));
    $("queues").replaceChildren(...Object.entries(data.queues || {}).map(([key, value]) => listRow(MODES[key] || key, number(value.queued) + " в очереди · " + number(value.workers) + " исполнителей")));
    $("active-count").textContent = number((data.active_runs || []).length); renderRuns("active-runs", data.active_runs || [], true); renderRuns("recent-runs", data.recent_runs || []);
  }
  async function refreshMetrics() { if (!state.settings || state.loading) return; const epoch = state.epoch; state.loading = true; $("refresh").disabled = true; window.clearTimeout(state.timer); try { const data = await api("metrics?days=" + encodeURIComponent($("days").value)); if (epoch === state.epoch) { renderMetrics(data); $("connection").textContent = "Обновлено " + new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(new Date()); } } catch (error) { if (epoch === state.epoch) notice(error.message, true); } finally { if (epoch === state.epoch) { state.loading = false; $("refresh").disabled = false; if (!document.hidden && state.settings) state.timer = window.setTimeout(refreshMetrics, 15000); } } }
  function openAuth() { $("auth-kind").value = state.auth?.kind || "bearer"; $("auth-dialog").showModal(); $("auth-secret").focus(); }
  $("auth-open").addEventListener("click", openAuth); $("access-login").addEventListener("click", openAuth); $("auth-close").addEventListener("click", () => $("auth-dialog").close()); $("auth-dialog").addEventListener("close", () => { $("auth-secret").value = ""; });
  $("auth-form").addEventListener("submit", (event) => { event.preventDefault(); const value = $("auth-secret").value.trim(); if (!value) return; resetAccess(); state.auth = { kind: $("auth-kind").value, value }; try { sessionStorage.setItem(AUTH_KEY, JSON.stringify(state.auth)); } catch (_) {} $("auth-dialog").close(); notice("", true); loadSettings(); });
  $("auth-clear").addEventListener("click", () => { resetAccess(); state.auth = null; try { sessionStorage.removeItem(AUTH_KEY); } catch (_) {} $("auth-dialog").close(); loadSettings(); });
  $("profile-mode").addEventListener("change", renderProfile); $("profile-effort").addEventListener("change", renderProfile); $("days").addEventListener("change", refreshMetrics); $("refresh").addEventListener("click", refreshMetrics); $("settings-form").addEventListener("submit", saveSettings);
  $("discard").addEventListener("click", () => { notice("", true); notice(""); loadSettings(); });
  window.addEventListener("beforeunload", (event) => { if (dirty()) { event.preventDefault(); event.returnValue = ""; } });
  window.addEventListener("pagehide", () => { window.clearTimeout(state.timer); for (const controller of state.requests) controller.abort(); });
  document.addEventListener("visibilitychange", () => { window.clearTimeout(state.timer); if (!document.hidden) refreshMetrics(); });
  loadSettings();
})();
