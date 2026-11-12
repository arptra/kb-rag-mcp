import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { accessError } from "./accessApi";
import { ManagementLogin, integrationDate, useManagementApi } from "./managementApi";
import type { RagIndex } from "./types";
import "./integrations.css";

interface Schedule { kind: "manual" | "interval" | "daily" | "weekly"; interval_minutes: number; time: string; weekday: number; timezone: string }
interface ConfluenceSource {
  id: string; name: string; index_id: string; base_url: string; root_page_url: string;
  deployment: "auto" | "cloud" | "data_center"; username: string | null; enabled: boolean;
  include_history: boolean; analyze_images: boolean; schedule: Schedule; credential_configured: boolean;
  paused: boolean; status: string; last_checked_at: string | null; last_success_at: string | null;
  next_check_at: string | null; last_error: string | null; page_count: number; revision_count: number; asset_count: number;
}
interface ConfluenceJob {
  id: string; source_id: string; index_id: string; type: string; status: string; phase: string;
  message: string; created_at: string; started_at: string | null; completed_at: string | null;
  progress: Record<string, unknown>; result: Record<string, unknown> | null; error: string | null;
}
interface ConfluenceCatalog { enabled?: boolean; sources: ConfluenceSource[]; jobs?: ConfluenceJob[]; can_manage: boolean; gigacode?: { available: boolean; error?: string | null } }
interface LogItem { cursor: string | number; timestamp: string; stage: string; level: string; message: string; details?: Record<string, unknown> }
interface LogPage { items: LogItem[]; next_cursor: string | number | null; has_more: boolean }
interface SourcePage { page_id: string; title: string; parent_id: string | null; version: string | number; source_updated_at: string | null; url: string; state: string; revision_id: string | null }
interface SourcePageList { pages: SourcePage[]; total: number; offset: number; limit: number; has_more: boolean }
type Request = <T>(path: string, init?: RequestInit) => Promise<T>;
const active = (status: string) => ["queued", "running", "cancelling"].includes(status);
const labels: Record<string, string> = { idle: "Готов", queued: "В очереди", running: "Выполняется", paused: "Приостановлен", error: "Ошибка", failed: "Ошибка", completed: "Завершено", completed_with_warnings: "С предупреждениями", cancelled: "Отменено", cancelling: "Отменяется" };

function SourcePages({ source, request, onClose }: { source: ConfluenceSource; request: Request; onClose: () => void }) {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<SourcePageList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError("");
    const params = new URLSearchParams({ offset: String(offset), limit: "50", query });
    void request<SourcePageList>(`/admin/api/confluence/sources/${encodeURIComponent(source.id)}/pages?${params}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setData(result); })
      .catch(caught => { if (!controller.signal.aborted) setError(accessError(caught)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [request, source.id, offset, query, revision]);
  const pageStates: Record<string, string> = { current: "Сохранена", access_lost: "Доступ потерян", unavailable: "Недоступна", removed: "Удалена", archived: "В архиве" };
  return <section className="panel integration-panel"><div className="integration-job-head"><div><span className="eyebrow">Обнаруженные страницы</span><h3>{source.name}</h3></div><button className="button quiet" onClick={onClose}>Скрыть страницы</button></div>
    <form className="integration-actions" onSubmit={event => { event.preventDefault(); setOffset(0); setQuery(draft.trim()); setRevision(current => current + 1); }}><label>Поиск по названию<input value={draft} onChange={event => setDraft(event.target.value)} placeholder="Название страницы" /></label><button className="button secondary" disabled={loading}>Найти</button><button type="button" className="button quiet" disabled={loading} onClick={() => setRevision(current => current + 1)}>Обновить</button></form>
    {error && <div className="form-error" role="alert">{error}</div>}
    {loading ? <p className="integration-muted">Загружаем страницы…</p> : !error && data && <><p className="integration-muted">Найдено: {data.total}. Родитель указан своим ID в Confluence. Сохранённые тексты и версии доступны в документах индекса.</p><div className="table-wrap"><table><thead><tr><th>Страница</th><th>Родитель</th><th>Версия</th><th>Изменена в источнике</th><th>Состояние</th></tr></thead><tbody>{data.pages.map(page => <tr key={page.page_id}><td>{/^https?:\/\//i.test(page.url) ? <a href={page.url} target="_blank" rel="noreferrer">{page.title || page.page_id} ↗</a> : page.title || page.page_id}<small>ID: {page.page_id}</small></td><td>{page.parent_id || "Корневая"}</td><td>{page.version || "—"}</td><td>{integrationDate(page.source_updated_at)}</td><td>{pageStates[page.state] || page.state}</td></tr>)}</tbody></table></div>{!data.pages.length && <div className="empty-state compact">Страницы не найдены</div>}<div className="integration-actions"><button className="button secondary" disabled={offset === 0} onClick={() => setOffset(current => Math.max(0, current - 50))}>Назад</button><small className="integration-muted">{data.total ? `${offset + 1}–${offset + data.pages.length} из ${data.total}` : "0 страниц"}</small><button className="button secondary" disabled={!data.has_more} onClick={() => setOffset(current => current + 50)}>Далее</button></div></>}
  </section>;
}

function ConfluenceLog({ job, request }: { job: ConfluenceJob; request: Request }) {
  const [items, setItems] = useState<LogItem[]>([]);
  const [error, setError] = useState("");
  const [following, setFollowing] = useState(true);
  const [revision, setRevision] = useState(0);
  const cursor = useRef<string | number | null>(null);
  useEffect(() => { cursor.current = null; setItems([]); setError(""); }, [job.id]);
  useEffect(() => {
    const controller = new AbortController(); let timer = 0;
    const poll = async () => {
      const params = new URLSearchParams({ limit: "200" });
      if (cursor.current !== null) params.set("cursor", String(cursor.current));
      try {
        const page = await request<LogPage>(`/admin/api/confluence/jobs/${encodeURIComponent(job.id)}/log?${params}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        cursor.current = page.next_cursor;
        setItems(current => {
          const merged = new Map(current.map(item => [String(item.cursor), item]));
          page.items.forEach(item => merged.set(String(item.cursor), item));
          return [...merged.values()].slice(-1000);
        }); setError("");
        if (page.has_more || (following && active(job.status))) timer = window.setTimeout(poll, page.has_more ? 100 : 2000);
      } catch (caught) {
        if (!controller.signal.aborted) { setError(accessError(caught)); if (following && active(job.status)) timer = window.setTimeout(poll, 5000); }
      }
    };
    void poll(); return () => { controller.abort(); window.clearTimeout(timer); };
  }, [job.id, job.status, request, following, revision]);
  return <div><div className="integration-log-controls"><label className="integration-check"><input type="checkbox" checked={following} onChange={event => setFollowing(event.target.checked)} /> Обновлять во время работы</label><button className="button quiet" onClick={() => setRevision(value => value + 1)}>Обновить журнал</button><small className="integration-muted">Последние 1000 записей · новые записи загружаются частями</small></div>{error && <div className="form-error">{error}</div>}<div className="integration-log" role="log" aria-live="off">{items.length ? items.map(item => <div key={String(item.cursor)}><b>{integrationDate(item.timestamp)} · {item.stage} · {item.level}</b>{"\n"}{item.message}{item.details && Object.keys(item.details).length > 0 ? `\n${JSON.stringify(item.details)}` : ""}{"\n"}</div>) : "Журнал пока пуст"}</div></div>;
}

function SourceForm({ source, indexes, initialIndex, request, gigacodeAvailable, onClose, onSaved }: {
  source: ConfluenceSource | null; indexes: RagIndex[]; initialIndex?: string; request: Request;
  gigacodeAvailable?: boolean; onClose: () => void; onSaved: () => void;
}) {
  const [name, setName] = useState(source?.name || "");
  const [index, setIndex] = useState(source?.index_id || initialIndex || "");
  const [base, setBase] = useState(source?.base_url || "");
  const [root, setRoot] = useState(source?.root_page_url || "");
  const [deployment, setDeployment] = useState<ConfluenceSource["deployment"]>(source?.deployment || "auto");
  const [username, setUsername] = useState(source?.username || "");
  const [token, setToken] = useState("");
  const [schedule, setSchedule] = useState<Schedule>(source?.schedule || { kind: "daily", interval_minutes: 60, time: "03:00", weekday: 0, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" });
  const [history, setHistory] = useState(source?.include_history ?? true);
  const [images, setImages] = useState(source?.analyze_images ?? true);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [check, setCheck] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const connectionRevision = useRef(0);
  let autoCloud = false;
  try { autoCloud = deployment === "auto" && new URL(base.trim()).hostname.toLowerCase().endsWith(".atlassian.net"); } catch { /* An incomplete URL is validated by the form. */ }
  const effectiveCloud = deployment === "cloud" || autoCloud;
  useEffect(() => {
    ++connectionRevision.current; setCheck(null); setError("");
  }, [base, root, deployment, username, token, index]);
  const payload = () => ({ name: name.trim() || `Confluence · ${new URL(base).hostname}`.slice(0, 120), index_id: index, base_url: base.trim(), root_page_url: root.trim(), deployment, username: username.trim() || null, ...(token ? { token } : {}), schedule, include_history: history, analyze_images: images, enabled: source?.enabled ?? true });
  const test = async () => {
    if (!formRef.current?.reportValidity()) return;
    const testedRevision = connectionRevision.current;
    setChecking(true); setError(""); setCheck(null);
    try {
      const result = await request<Record<string, unknown>>("/admin/api/confluence/test", { method: "POST", body: JSON.stringify({ ...payload(), ...(source ? { source_id: source.id } : {}) }) });
      if (testedRevision === connectionRevision.current) setCheck(typeof result.message === "string" ? result.message : `Подключение проверено${result.title ? `: ${String(result.title)}` : ""}`);
    } catch (caught) { if (testedRevision === connectionRevision.current) setError(accessError(caught)); } finally { setChecking(false); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!index || busy) return;
    setBusy(true); setError("");
    try { await request(source ? `/admin/api/confluence/sources/${encodeURIComponent(source.id)}` : "/admin/api/confluence/sources", { method: source ? "PATCH" : "POST", body: JSON.stringify(payload()) }); setToken(""); onSaved(); }
    catch (caught) { setError(accessError(caught)); } finally { setBusy(false); }
  };
  return <div className="modal-backdrop" onMouseDown={() => { if (!busy && !checking) onClose(); }} role="presentation"><section className="modal confluence-form" role="dialog" aria-modal="true" aria-labelledby="confluence-form-title" onMouseDown={event => event.stopPropagation()}>
    <header className="modal-header"><div><span className="eyebrow">Confluence → выбранный индекс</span><h2 id="confluence-form-title">{source ? "Настроить источник" : "Подключить Confluence"}</h2></div><button className="icon-button" aria-label="Закрыть" disabled={busy || checking} onClick={onClose}>×</button></header>
    <form ref={formRef} className="modal-form" onSubmit={event => void submit(event)}>
      <div className="confluence-form-grid"><div className="modal-form">
        <label>Название · необязательно<input maxLength={120} value={name} onChange={event => setName(event.target.value)} placeholder="Заполнится из адреса Confluence" /></label>
        <label>Целевой индекс · обязательно<select required disabled={Boolean(source)} value={index} onChange={event => setIndex(event.target.value)}><option value="">Выберите индекс</option>{indexes.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><small>{source ? "Для другого индекса создайте новое подключение." : "Страницы, история и описания изображений будут доступны читателям этого индекса."}</small></label>
        <label>Адрес Confluence<input required type="url" value={base} onChange={event => setBase(event.target.value)} placeholder="https://confluence.company.ru" /></label>
        <label>Корневая страница<input required type="url" value={root} onChange={event => setRoot(event.target.value)} placeholder="https://confluence.company.ru/pages/viewpage.action?pageId=123" /></label>
        <label>Тип Confluence<select value={deployment} onChange={event => setDeployment(event.target.value as ConfluenceSource["deployment"])}><option value="auto">Определить автоматически</option><option value="data_center">Data Center · персональный токен</option><option value="cloud">Cloud · API token</option></select></label>
        {deployment !== "data_center" && <label>Email Atlassian{effectiveCloud ? " · обязательно" : " · для Cloud"}<input type="text" inputMode="email" maxLength={320} required={effectiveCloud} pattern={effectiveCloud ? ".*\\S.*" : undefined} value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" /><small>{autoCloud ? "По адресу atlassian.net определён Confluence Cloud. Нужен email учётной записи, для которой выдан API token." : effectiveCloud ? "Укажите email учётной записи Atlassian, для которой выдан API token." : "Для Confluence Cloud нужны email и API token. Для Data Center достаточно персонального токена."}</small></label>}
        <label>{source ? "Новый токен · оставить пустым, чтобы сохранить" : "Токен"}<input type="password" required={!source} value={token} onChange={event => setToken(event.target.value)} autoComplete="new-password" /><small>Токен передаётся серверу и не возвращается в интерфейс.</small></label>
      </div><div className="modal-form">
        <label>Проверять обновления<select value={schedule.kind} onChange={event => setSchedule(current => ({ ...current, kind: event.target.value as Schedule["kind"] }))}><option value="manual">Только вручную</option><option value="interval">С интервалом</option><option value="daily">Ежедневно</option><option value="weekly">Еженедельно</option></select></label>
        {schedule.kind === "interval" && <label>Интервал, минут<input required type="number" min={1} max={525600} value={schedule.interval_minutes} onChange={event => setSchedule(current => ({ ...current, interval_minutes: Number(event.target.value) }))} /></label>}
        {schedule.kind === "weekly" && <label>День<select value={schedule.weekday} onChange={event => setSchedule(current => ({ ...current, weekday: Number(event.target.value) }))}>{["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"].map((day, i) => <option key={day} value={i}>{day}</option>)}</select></label>}
        {["daily", "weekly"].includes(schedule.kind) && <label>Время<input required type="time" value={schedule.time} onChange={event => setSchedule(current => ({ ...current, time: event.target.value }))} /></label>}
        {schedule.kind !== "manual" && <label>Часовой пояс<input required list="confluence-timezones" value={schedule.timezone} onChange={event => setSchedule(current => ({ ...current, timezone: event.target.value }))} placeholder="Europe/Moscow" /><datalist id="confluence-timezones"><option value="Europe/Moscow" /><option value="UTC" /><option value="Asia/Yekaterinburg" /><option value="Asia/Novosibirsk" /></datalist></label>}
        <label className="integration-check"><input type="checkbox" checked={history} onChange={event => setHistory(event.target.checked)} /><span>Загрузить доступные прошлые версии<small>История изображений зависит от доступности вложений в Confluence.</small></span></label>
        <label className="integration-check"><input type="checkbox" checked={images} onChange={event => setImages(event.target.checked)} /><span>Обрабатывать изображения через GigaCode<small>SVG, BMP, JPG, JPEG, PNG</small></span></label>
        {images && <div className={`callout ${gigacodeAvailable === false ? "integration-error" : ""}`}>{gigacodeAvailable === false ? "GigaCode сейчас недоступен. Статус обработки изображений будет виден в журнале." : gigacodeAvailable ? "GigaCode доступен на сервере" : "Доступность GigaCode проверяется на сервере"}</div>}
        <small>Синхронизация выполняется на сервере. Закрытие страницы её не останавливает.</small>
      </div></div>
      {error && <div className="form-error" role="alert">{error}</div>}{check && <div className="access-notice" role="status">{check}</div>}
      <div className="modal-actions"><button type="button" className="button quiet" disabled={busy || checking} onClick={onClose}>Отмена</button><button type="button" className="button secondary" disabled={busy || checking} onClick={() => void test()}>{checking ? "Проверяем…" : "Проверить доступ"}</button><button className="button primary" disabled={busy || checking || !index}>{busy ? "Сохраняем…" : source ? "Сохранить" : "Подключить и синхронизировать"}</button></div>
    </form>
  </section></div>;
}

export default function ConfluenceApp({ password, indexes, indexId, onChanged }: { password: string; indexes: RagIndex[]; indexId?: string; onChanged?: () => void }) {
  const { request, login, revision } = useManagementApi(password);
  const [catalog, setCatalog] = useState<ConfluenceCatalog | null>(null);
  const [jobs, setJobs] = useState<ConfluenceJob[]>([]);
  const [editing, setEditing] = useState<ConfluenceSource | "new" | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState("");
  const [pagesSourceId, setPagesSourceId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const params = new URLSearchParams(); if (indexId) params.set("index_id", indexId);
      const data = await request<ConfluenceCatalog>(`/admin/api/confluence?${params}`, { signal }); setCatalog(data);
      if (data.enabled === false) { setJobs([]); setEditing(null); setLoadError(""); return; }
      const jobParams = new URLSearchParams({ limit: "30" }); if (sourceFilter) jobParams.set("source_id", sourceFilter); if (indexId) jobParams.set("index_id", indexId);
      const history = await request<{ jobs: ConfluenceJob[] }>(`/admin/api/confluence/jobs?${jobParams}`, { signal }); setJobs(history.jobs.filter(job => !indexId || job.index_id === indexId)); setLoadError("");
    } catch (caught) { if (!signal?.aborted) setLoadError(accessError(caught)); }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [request, indexId, sourceFilter]);
  useEffect(() => {
    const controller = new AbortController(); let timer = 0;
    const poll = async () => { await load(controller.signal); if (!controller.signal.aborted) timer = window.setTimeout(poll, 4000); };
    void poll(); return () => { controller.abort(); window.clearTimeout(timer); };
  }, [load, revision]);
  const mutate = async (path: string, body: unknown, success: string) => {
    setBusy(true); setError("");
    try { await request(path, { method: "POST", body: JSON.stringify(body) }); setNotice(success); await load(); onChanged?.(); }
    catch (caught) { setError(accessError(caught)); } finally { setBusy(false); }
  };
  const sources = (catalog?.sources ?? []).filter(source => !indexId || source.index_id === indexId);
  const pagesSource = sources.find(source => source.id === pagesSourceId);
  const sourceBusy = (source: ConfluenceSource) => active(source.status) || jobs.some(job => job.source_id === source.id && active(job.status));
  if (catalog?.enabled === false) return <div className="integration-area confluence-sources"><section className="panel integration-panel"><h3>Источники Confluence</h3><p className="integration-muted" role="status">Интеграция Confluence отключена на сервере.</p></section></div>;
  return <div className="integration-area confluence-sources">
    <section className="panel"><div className="panel-header"><div><span className="eyebrow">Документация по API</span><h3>Источники Confluence</h3></div>{catalog?.can_manage && <button className="button primary" onClick={() => setEditing("new")}>＋ Подключить Confluence</button>}</div>
      {(error || loadError) && <div className="form-error" role="alert">{error || loadError}</div>}{notice && <div className="access-notice" role="status">{notice}</div>}
      {loading ? <div className="empty-state compact">Загружаем источники…</div> : sources.length ? <div className="table-wrap"><table><thead><tr><th>Источник / корень</th><th>Индекс</th><th>Документы</th><th>Проверки</th><th>Состояние</th><th /></tr></thead><tbody>{sources.map(source => <tr key={source.id}><td><b>{source.name}</b><small><a href={source.root_page_url} target="_blank" rel="noreferrer">Корневая страница ↗</a></small></td><td>{indexes.find(index => index.id === source.index_id)?.name || source.index_id}</td><td>{source.page_count} страниц<small>{source.revision_count} версий · {source.asset_count} вложений</small></td><td><small>Последняя: {integrationDate(source.last_checked_at)}</small><small>Успешная: {integrationDate(source.last_success_at)}</small><small>Следующая: {source.paused ? "На паузе" : integrationDate(source.next_check_at)}</small></td><td><span className="tag">{labels[source.status] || source.status}</span>{source.last_error && <small className="integration-error">{source.last_error}</small>}</td><td><div className="integration-actions"><button className="button quiet" onClick={() => setPagesSourceId(source.id)}>Страницы</button><button className="button quiet" onClick={() => setSourceFilter(source.id)}>Журнал</button>{catalog?.can_manage && <><button className="button quiet" disabled={busy || sourceBusy(source)} onClick={() => void mutate(`/admin/api/confluence/sources/${encodeURIComponent(source.id)}/sync`, {}, "Синхронизация поставлена в очередь")}>Проверить</button><button className="button quiet" disabled={busy || sourceBusy(source)} onClick={() => setEditing(source)}>Настроить</button><button className="button quiet" disabled={busy} onClick={() => void mutate(`/admin/api/confluence/sources/${encodeURIComponent(source.id)}/pause`, { paused: !source.paused }, source.paused ? "Расписание возобновлено" : "Расписание приостановлено")}>{source.paused ? "Возобновить" : "Пауза"}</button><button className="button quiet" disabled={busy || sourceBusy(source)} onClick={() => { if (window.confirm(`Отключить источник «${source.name}»? Сохранённые документы и история останутся в индексе.`)) void mutate(`/admin/api/confluence/sources/${encodeURIComponent(source.id)}/delete`, {}, "Источник отключён, архив сохранён"); }}>Отключить</button></>}</div></td></tr>)}</tbody></table></div> : <div className="empty-state compact"><h3>Confluence ещё не подключён</h3><p>Выберите индекс, укажите сервер, корневую страницу и токен.</p></div>}
    </section>
    {pagesSource && <SourcePages key={pagesSource.id} source={pagesSource} request={request} onClose={() => setPagesSourceId(null)} />}
    {catalog && !catalog.can_manage && <ManagementLogin onLogin={login} />}
    {jobs.length > 0 && <section className="panel integration-panel"><div className="integration-job-head"><h3>Синхронизация и индексация</h3><label>Источник<select value={sourceFilter} onChange={event => setSourceFilter(event.target.value)}><option value="">Все источники</option>{sources.map(source => <option key={source.id} value={source.id}>{source.name}</option>)}</select></label></div>{jobs.map(job => {
      const progress = job.progress || {}; const count = (key: string) => typeof progress[key] === "number" ? Number(progress[key]) : null;
      const processedChunks = count("completed") ?? count("processed") ?? count("processed_chunks");
      const processed = processedChunks ?? count("pages_done");
      const total = processedChunks !== null ? count("total") ?? count("total_chunks") : count("pages_total");
      return <article className="integration-job-row" key={job.id}><div className="integration-job-head"><div><b>{job.message || job.phase || "Синхронизация Confluence"}</b><small className="integration-muted"> · {labels[job.status] || job.status}</small><div className="integration-muted">{sources.find(source => source.id === job.source_id)?.name || job.source_id} · {indexes.find(index => index.id === job.index_id)?.name || job.index_id} · {integrationDate(job.completed_at || job.started_at || job.created_at)}</div></div><div className="integration-actions"><button className="button quiet" onClick={() => setExpanded(expanded === job.id ? null : job.id)}>{expanded === job.id ? "Скрыть журнал" : "Журнал"}</button>{catalog?.can_manage && active(job.status) && <button className="button quiet" disabled={busy || job.status === "cancelling"} onClick={() => void mutate(`/admin/api/confluence/jobs/${encodeURIComponent(job.id)}/cancel`, {}, "Отмена запрошена")}>Отменить</button>}</div></div>
        {total !== null && total > 0 && processed !== null && <progress className="integration-progress" value={processed} max={total} aria-label={processedChunks !== null ? "Обработанные фрагменты" : "Проверенные страницы"} />}
        <div className="integration-facts">{[[processedChunks !== null ? "Обработано фрагментов" : "Проверено страниц", processed !== null ? `${processed}${total !== null ? ` / ${total}` : ""}` : null], ["Из кэша", count("reused") ?? count("reused_chunks")], ["Рассчитано", count("computed") ?? count("computed_chunks")], ["Опубликовано", count("published") ?? count("published_chunks")]].map(([label, value]) => value !== null ? <span key={String(label)}>{label}<b>{value}</b></span> : null)}</div>
        {job.error && <div className="integration-error">{job.error}</div>}{expanded === job.id && <ConfluenceLog job={job} request={request} />}
      </article>;
    })}<p className="integration-muted">Задачи выполняются на сервере. Закрытие вкладки не прерывает обработку; журнал и сохранённый прогресс доступны после возвращения.</p></section>}
    {editing && <SourceForm source={editing === "new" ? null : editing} indexes={indexes} initialIndex={indexId} request={request} gigacodeAvailable={catalog?.gigacode?.available} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setNotice("Источник сохранён"); void load(); onChanged?.(); }} />}
  </div>;
}
