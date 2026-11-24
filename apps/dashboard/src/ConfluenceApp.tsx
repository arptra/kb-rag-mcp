import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { accessError } from "./accessApi";
import { ApiError } from "./api";
import { ManagementLogin, integrationDate, useManagementApi } from "./managementApi";
import type { RagIndex } from "./types";
import "./integrations.css";

interface Schedule { kind: "manual" | "interval" | "daily" | "weekly"; interval_minutes: number; time: string; weekday: number; timezone: string }
interface ConfluenceSource {
  id: string; name: string; index_id: string; base_url: string; root_page_url: string;
  deployment: "auto" | "cloud" | "data_center"; username: string | null; enabled: boolean;
  analyze_images: boolean; schedule: Schedule; credential_configured: boolean;
  paused: boolean; status: string; last_checked_at: string | null; last_success_at: string | null;
  next_check_at: string | null; last_error: string | null; page_count: number; asset_count: number;
  active_job?: ConfluenceJob | null;
}
interface ConfluenceCredential {
  source_id: string; name: string; base_url: string; deployment: ConfluenceSource["deployment"];
  username: string | null; auth_mode: "basic" | "bearer"; last_success_at: string | null;
}
interface ConfluenceJob {
  id: string; source_id: string; index_id: string; type: string; status: string; phase: string;
  operation?: "sync" | "analyze_images";
  message: string; created_at: string; started_at: string | null; completed_at: string | null;
  progress: Record<string, unknown> & { authentication_required?: boolean; authentication_url?: string | null; authentication_requested_at?: string | null }; result: Record<string, unknown> | null; error: string | null;
}
interface ConfluenceCatalog { enabled?: boolean; sources: ConfluenceSource[]; jobs?: ConfluenceJob[]; can_manage: boolean; gigacode?: { available: boolean; error?: string | null } }
interface LogItem { cursor: string | number; timestamp: string; stage: string; level: string; message: string; details?: Record<string, unknown> }
interface LogPage { items: LogItem[]; next_cursor: string | number | null; has_more: boolean }
interface SourcePage { page_id: string; title: string; parent_id: string | null; version: string | number; source_updated_at: string | null; url: string; state: string; revision_id: string | null }
interface SourcePageList { pages: SourcePage[]; total: number; offset: number; limit: number; has_more: boolean }
interface FailedPage { page_id: string; title: string; url: string; stage: string; task_kind: "page" | "discovery"; attempts: number; error: string; error_type: string; status_code: number | null; status: "retry" | "failed" }
interface FailurePage { items: FailedPage[]; total: number; offset: number; limit: number; has_more: boolean }
type Request = <T>(path: string, init?: RequestInit) => Promise<T>;
const active = (status: string) => ["queued", "running", "cancelling"].includes(status);
const labels: Record<string, string> = { idle: "Готов", queued: "В очереди", running: "Выполняется", paused: "Приостановлен", error: "Ошибка", failed: "Ошибка", completed: "Завершено", completed_with_warnings: "С предупреждениями", cancelled: "Отменено", cancelling: "Отменяется" };
const stageLabels: Record<string, string> = { authentication: "Ожидание входа в GigaCode", discovery: "Поиск дочерних страниц", discovery_retry: "Повторный поиск дочерних страниц", fetch: "Загрузка страницы", attachments: "Загрузка вложений", images: "Обработка изображений", normalization: "Обработка текста", archive: "Сохранение страницы", checkpoint: "Сохранение страницы", indexing: "Индексация", publication: "Публикация", retry_wait: "Ожидание повторной попытки", retry: "Повторная загрузка" };
const awaitingAuthentication = (job: ConfluenceJob) => job.status === "running" && job.progress?.authentication_required === true;

function authenticationUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const candidate = value.trim();
  if (!candidate || /[\u0000-\u0020\u007f\\]/.test(candidate)) return null;
  try {
    const url = new URL(candidate);
    return ["http:", "https:"].includes(url.protocol) && Boolean(url.hostname) && !url.username && !url.password ? candidate : null;
  } catch { return null; }
}

function confluenceServerScope(value: string, deployment: ConfluenceSource["deployment"]): string | null {
  try {
    const candidate = value.trim();
    if (/[\u0000-\u0020\u007f\\]/.test(candidate)) return null;
    const url = new URL(candidate);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
    const cloud = deployment === "cloud" || (deployment === "auto" && url.hostname.toLowerCase().endsWith(".atlassian.net"));
    let path = url.pathname.replace(/\/+$/, "");
    if (cloud && !path) path = "/wiki";
    return `${url.origin}${path}${cloud ? "/api/v2" : "/rest/api"}`;
  } catch { return null; }
}

function credentialMatchesServer(credential: ConfluenceCredential, base: string): boolean {
  const scope = confluenceServerScope(base, credential.deployment);
  return scope !== null && scope === confluenceServerScope(credential.base_url, credential.deployment);
}

function confluenceConnectionError(error: unknown): string {
  if (error instanceof ApiError && (error.details?.error_code === "authentication" || error.details?.upstream_status === 401)) {
    return "Confluence не принял выбранные данные доступа. Выберите другой сохранённый доступ или введите новый токен. Сохранённые данные не удалены.";
  }
  return accessError(error);
}

function GigaCodeAuthentication({ job, canManage }: { job: ConfluenceJob; canManage: boolean }) {
  const url = canManage ? authenticationUrl(job.progress.authentication_url) : null;
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  useEffect(() => { setCopied(false); setCopyError(""); }, [url]);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const copy = async () => {
    if (!url) return;
    try { await navigator.clipboard.writeText(url); setCopied(true); setCopyError(""); }
    catch { setCopyError("Буфер обмена недоступен. Скопируйте ссылку из поля ниже."); }
  };
  return <section className="confluence-authentication" aria-label="Вход в GigaCode">
    <div role="status"><strong>GigaCode ожидает вход через браузер</strong><p>{canManage ? "Откройте ссылку и завершите вход. После авторизации обработка продолжится автоматически." : "Войдите как администратор, чтобы увидеть ссылку и пройти авторизацию GigaCode."}</p></div>
    {canManage && (url ? <>
      <div className="integration-actions"><a className="button primary" href={url} target="_blank" rel="noopener noreferrer">Открыть страницу входа ↗</a><button className="button secondary" onClick={() => void copy()}>{copied ? "Скопировано" : "Скопировать ссылку"}</button></div>
      {copyError && <p className="integration-error" role="alert">{copyError}</p>}
      <details className="confluence-authentication-url" open={Boolean(copyError)}><summary>Полная ссылка для входа</summary><textarea aria-label="Ссылка для входа в GigaCode" value={url} readOnly rows={3} spellCheck={false} onFocus={event => event.currentTarget.select()} /></details>
    </> : <p className="integration-muted">Ссылка для входа пока недоступна. Статус обновляется автоматически.</p>)}
    {job.progress.authentication_requested_at && <small className="integration-muted">Ожидание с {integrationDate(job.progress.authentication_requested_at)}</small>}
  </section>;
}

function ConfluenceFailures({ job, request }: { job: ConfluenceJob; request: Request }) {
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<FailurePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); let timer = 0;
    setData(null); setLoading(true); setError("");
    const poll = async () => {
      try {
        const params = new URLSearchParams({ offset: String(offset), limit: "50" });
        const page = await request<FailurePage>(`/admin/api/confluence/jobs/${encodeURIComponent(job.id)}/failures?${params}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (offset > 0 && !page.items.length) { setOffset(Math.max(0, Math.ceil(page.total / 50) - 1) * 50); return; }
        setData(page); setError("");
      } catch (caught) { if (!controller.signal.aborted) setError(accessError(caught)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
      if (!controller.signal.aborted && active(job.status)) timer = window.setTimeout(poll, 4000);
    };
    void poll(); return () => { controller.abort(); window.clearTimeout(timer); };
  }, [job.id, job.status, request, offset, revision]);
  return <section className="confluence-failures" aria-label="Ошибки загрузки">
    <div className="integration-job-head"><h4>Ошибки загрузки</h4><button className="button quiet" disabled={loading} onClick={() => setRevision(value => value + 1)}>Обновить список</button></div>
    <p className="integration-muted">{active(job.status) ? "Остальные страницы продолжают обрабатываться. Для ошибок предусмотрены ещё два прохода в конце; список обновляется по мере повторных попыток." : job.operation === "analyze_images" ? "Здесь остаются страницы, изображения которых не удалось обработать. Для новой попытки нажмите «Распознать изображения» у источника." : "Здесь остаются страницы и ветки, которые не удалось загрузить. Для новой попытки нажмите «Синхронизировать» у источника."}</p>
    {error && <div className="form-error" role="alert">{error}</div>}
    {loading ? <p className="integration-muted">Загружаем список…</p> : data && <>
      {data.items.length > 0 ? <div className="table-wrap"><table><thead><tr><th>Страница / ветка</th><th>Этап и ошибка</th><th>Попытки</th><th>Состояние</th></tr></thead><tbody>{data.items.map(item => <tr key={`${item.task_kind}:${item.page_id}`}>
        <td>{/^https?:\/\//i.test(item.url) ? <a href={item.url} target="_blank" rel="noreferrer">{item.title || item.page_id} ↗</a> : item.title || item.page_id}<small>ID: {item.page_id}</small>{item.task_kind === "discovery" && <small>Не удалось прочитать ветку. Часть дочерних страниц может быть не обнаружена.</small>}</td>
        <td><b>{stageLabels[item.stage] || item.stage}</b><div className="confluence-failure-message">{item.error}</div><small>{[item.error_type, item.status_code != null ? `HTTP ${item.status_code}` : null].filter(Boolean).join(" · ")}</small></td>
        <td>{item.attempts}</td><td><span className="tag">{item.status === "retry" && active(job.status) ? "В очереди повтора" : "Не загружена"}</span></td>
      </tr>)}</tbody></table></div> : <div className="empty-state compact">Ошибок загрузки не осталось</div>}
      <div className="integration-actions"><button className="button secondary" disabled={offset === 0} onClick={() => setOffset(current => Math.max(0, current - 50))}>Назад</button><small className="integration-muted">{data.total ? `${offset + 1}–${offset + data.items.length} из ${data.total}` : "0 ошибок"}</small><button className="button secondary" disabled={!data.has_more} onClick={() => setOffset(current => current + 50)}>Далее</button></div>
    </>}
  </section>;
}

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
    {loading ? <p className="integration-muted">Загружаем страницы…</p> : !error && data && <><p className="integration-muted">Найдено: {data.total}. Родитель указан своим ID в Confluence. Актуальные тексты доступны в документах индекса.</p><div className="table-wrap"><table><thead><tr><th>Страница</th><th>Родитель</th><th>Версия</th><th>Изменена в источнике</th><th>Состояние</th></tr></thead><tbody>{data.pages.map(page => <tr key={page.page_id}><td>{/^https?:\/\//i.test(page.url) ? <a href={page.url} target="_blank" rel="noreferrer">{page.title || page.page_id} ↗</a> : page.title || page.page_id}<small>ID: {page.page_id}</small></td><td>{page.parent_id || "Корневая"}</td><td>{page.version || "—"}</td><td>{integrationDate(page.source_updated_at)}</td><td>{pageStates[page.state] || page.state}</td></tr>)}</tbody></table></div>{!data.pages.length && <div className="empty-state compact">Страницы не найдены</div>}<div className="integration-actions"><button className="button secondary" disabled={offset === 0} onClick={() => setOffset(current => Math.max(0, current - 50))}>Назад</button><small className="integration-muted">{data.total ? `${offset + 1}–${offset + data.pages.length} из ${data.total}` : "0 страниц"}</small><button className="button secondary" disabled={!data.has_more} onClick={() => setOffset(current => current + 50)}>Далее</button></div></>}
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
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [check, setCheck] = useState<string | null>(null);
  const [credentials, setCredentials] = useState<ConfluenceCredential[]>([]);
  const [credentialsLoading, setCredentialsLoading] = useState(true);
  const [credentialsError, setCredentialsError] = useState("");
  const [credentialsRevision, setCredentialsRevision] = useState(0);
  const [credentialId, setCredentialId] = useState(source?.credential_configured ? source.id : "");
  const [manualCredentials, setManualCredentials] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  const connectionRevision = useRef(0);
  const successfulRevision = useRef<number | null>(null);
  const successfulAt = useRef(0);
  const connectionEdited = useRef(false);
  const operation = useRef<AbortController | null>(null);
  useEffect(() => () => operation.current?.abort(), []);
  useEffect(() => {
    const controller = new AbortController();
    setCredentialsLoading(true); setCredentialsError("");
    void request<{ credentials: ConfluenceCredential[] }>("/admin/api/confluence/credentials", { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setCredentials(result.credentials); })
      .catch(caught => { if (!controller.signal.aborted) setCredentialsError(accessError(caught)); })
      .finally(() => { if (!controller.signal.aborted) setCredentialsLoading(false); });
    return () => controller.abort();
  }, [request, credentialsRevision]);
  const availableCredentials = useMemo(() => {
    const saved = [...credentials];
    if (source?.credential_configured && !saved.some(item => item.source_id === source.id)) {
      saved.push({ source_id: source.id, name: source.name, base_url: source.base_url, deployment: source.deployment, username: source.username, auth_mode: source.deployment !== "data_center" && source.username ? "basic" : "bearer", last_success_at: source.last_success_at });
    }
    const lastSuccess = (value: string | null) => value ? Date.parse(value) || 0 : 0;
    return saved.sort((left, right) => lastSuccess(right.last_success_at) - lastSuccess(left.last_success_at) || left.name.localeCompare(right.name));
  }, [credentials, source]);
  const selectedCredential = availableCredentials.find(item => item.source_id === credentialId && credentialMatchesServer(item, base)) ?? null;
  useEffect(() => {
    if (busy || checking || manualCredentials || token) return;
    if (availableCredentials.some(item => item.source_id === credentialId && credentialMatchesServer(item, base))) return;
    const serverScopes = new Set(availableCredentials.map(item => confluenceServerScope(item.base_url, item.deployment)));
    const candidate = !source && !base.trim() && !connectionEdited.current && !credentialsLoading && serverScopes.size === 1 && !serverScopes.has(null)
      ? availableCredentials[0]
      : availableCredentials.find(item => credentialMatchesServer(item, base));
    if (candidate) {
      setCredentialId(candidate.source_id); setDeployment(candidate.deployment); setUsername(candidate.username || "");
      if (!base.trim()) setBase(candidate.base_url);
    } else if (credentialId) setCredentialId("");
  }, [availableCredentials, base, credentialId, manualCredentials, token, source, credentialsLoading, busy, checking]);
  const chooseCredential = (id: string) => {
    connectionEdited.current = true; setCredentialId(id); setManualCredentials(!id); setToken("");
    const chosen = availableCredentials.find(item => item.source_id === id);
    if (chosen) { setBase(chosen.base_url); setDeployment(chosen.deployment); setUsername(chosen.username || ""); }
  };
  let autoCloud = false;
  try { autoCloud = deployment === "auto" && new URL(base.trim()).hostname.toLowerCase().endsWith(".atlassian.net"); } catch { /* An incomplete URL is validated by the form. */ }
  const effectiveCloud = deployment === "cloud" || autoCloud;
  useEffect(() => {
    ++connectionRevision.current; successfulRevision.current = null; setCheck(null); setError("");
  }, [base, root, deployment, username, token, index, credentialId, selectedCredential?.base_url, selectedCredential?.deployment, selectedCredential?.username]);
  const payload = () => ({ name: name.trim() || `Confluence · ${new URL(base).hostname}`.slice(0, 120), index_id: index, base_url: base.trim(), root_page_url: root.trim(), deployment: selectedCredential?.deployment ?? deployment, username: selectedCredential ? selectedCredential.username : deployment === "data_center" ? null : username.trim() || null, ...(selectedCredential ? { credential_source_id: selectedCredential.source_id } : token ? { token } : {}), schedule, include_history: false, analyze_images: false, enabled: source?.enabled ?? true });
  const verifyConnection = async (body: ReturnType<typeof payload>, testedRevision: number, signal: AbortSignal) => {
    const result = await request<Record<string, unknown>>("/admin/api/confluence/test", { method: "POST", body: JSON.stringify({ ...body, ...(source ? { source_id: source.id } : {}) }), signal });
    if (signal.aborted || testedRevision !== connectionRevision.current) return false;
    successfulRevision.current = testedRevision;
    successfulAt.current = Date.now();
    const title = typeof result.root_page_title === "string" ? result.root_page_title : typeof result.title === "string" ? result.title : "";
    setCheck(title ? `Доступ к странице «${title}» проверен` : typeof result.message === "string" ? result.message : "Подключение проверено");
    return true;
  };
  const test = async () => {
    if (busy || checking || !formRef.current?.reportValidity()) return;
    const testedRevision = connectionRevision.current;
    const controller = new AbortController(); operation.current = controller;
    successfulRevision.current = null;
    setChecking(true); setError(""); setCheck(null);
    try {
      await verifyConnection(payload(), testedRevision, controller.signal);
    } catch (caught) { if (!controller.signal.aborted && testedRevision === connectionRevision.current) setError(confluenceConnectionError(caught)); }
    finally { if (!controller.signal.aborted) setChecking(false); if (operation.current === controller) operation.current = null; }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!index || busy || checking || !formRef.current?.reportValidity()) return;
    const submittedRevision = connectionRevision.current;
    const controller = new AbortController(); operation.current = controller;
    setBusy(true); setError("");
    try {
      const body = payload();
      if (selectedCredential && (successfulRevision.current !== submittedRevision || Date.now() - successfulAt.current > 60_000)) {
        setChecking(true); setCheck(null);
        if (!await verifyConnection(body, submittedRevision, controller.signal)) return;
        setChecking(false);
      }
      if (controller.signal.aborted || submittedRevision !== connectionRevision.current) return;
      await request(source ? `/admin/api/confluence/sources/${encodeURIComponent(source.id)}` : "/admin/api/confluence/sources", { method: source ? "PATCH" : "POST", body: JSON.stringify(body), signal: controller.signal });
      if (!controller.signal.aborted) { setToken(""); onSaved(); }
    } catch (caught) { successfulRevision.current = null; if (!controller.signal.aborted && submittedRevision === connectionRevision.current) setError(confluenceConnectionError(caught)); }
    finally { if (!controller.signal.aborted) { setBusy(false); setChecking(false); } if (operation.current === controller) operation.current = null; }
  };
  return <div className="modal-backdrop" onMouseDown={() => { if (!busy && !checking) onClose(); }} role="presentation"><section className="modal confluence-form" role="dialog" aria-modal="true" aria-labelledby="confluence-form-title" onMouseDown={event => event.stopPropagation()}>
    <header className="modal-header"><div><span className="eyebrow">Confluence → выбранный индекс</span><h2 id="confluence-form-title">{source ? "Настроить источник" : "Подключить Confluence"}</h2></div><button className="icon-button" aria-label="Закрыть" disabled={busy || checking} onClick={onClose}>×</button></header>
    <form ref={formRef} className="modal-form" onSubmit={event => void submit(event)}>
      <fieldset disabled={busy || checking} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}><div className="confluence-form-grid"><div className="modal-form">
        <label>Название · необязательно<input maxLength={120} value={name} onChange={event => setName(event.target.value)} placeholder="Заполнится из адреса Confluence" /></label>
        <label>Целевой индекс · обязательно<select required disabled={Boolean(source)} value={index} onChange={event => setIndex(event.target.value)}><option value="">Выберите индекс</option>{indexes.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><small>{source ? "Для другого индекса создайте новое подключение." : "Актуальные страницы и описания изображений будут доступны читателям этого индекса."}</small></label>
        <label>Данные доступа<select value={selectedCredential?.source_id ?? ""} onChange={event => chooseCredential(event.target.value)}><option value="">Ввести новый токен</option>{availableCredentials.map(item => <option key={item.source_id} value={item.source_id}>{item.name} · {item.base_url} · {item.auth_mode === "basic" ? item.username || "Atlassian" : "Персональный токен"}</option>)}</select><small>{credentialsLoading ? "Загружаем сохранённые подключения… Новый токен можно ввести вручную." : selectedCredential ? "Используем сохранённый на сервере токен. Для другой корневой страницы вводить его повторно не нужно." : "Для знакомого сервера доступ подбирается автоматически. Можно выбрать другое подключение или ввести новый токен."}</small></label>
        {credentialsError && <div className="callout"><p>Не удалось загрузить сохранённые подключения: {credentialsError}. Можно ввести токен вручную.</p><button type="button" className="button quiet" disabled={credentialsLoading} onClick={() => setCredentialsRevision(value => value + 1)}>Повторить загрузку</button></div>}
        <label>Адрес Confluence<input required type="url" value={base} onChange={event => { connectionEdited.current = true; setBase(event.target.value); }} placeholder="https://confluence.company.ru" /></label>
        <label>Корневая страница<input required type="url" value={root} onChange={event => setRoot(event.target.value)} placeholder="https://confluence.company.ru/pages/viewpage.action?pageId=123" /></label>
        {selectedCredential ? <div className="callout"><b>{selectedCredential.auth_mode === "basic" ? "Email и токен" : "Персональный токен"}</b>{selectedCredential.username && <p>Учётная запись: {selectedCredential.username}</p>}<small>Источник доступа: {selectedCredential.name}. Последняя успешная синхронизация: {integrationDate(selectedCredential.last_success_at)}. Перед сохранением проверим доступ к выбранной корневой странице.</small>{source?.id !== selectedCredential.source_id && <p className="integration-muted">Данные исходного подключения не изменятся.</p>}</div> : <>
        <label>Тип Confluence<select value={deployment} onChange={event => { connectionEdited.current = true; setManualCredentials(true); setDeployment(event.target.value as ConfluenceSource["deployment"]); }}><option value="auto">Определить автоматически</option><option value="data_center">Data Center · персональный токен</option><option value="cloud">Cloud · API token</option></select></label>
        {deployment !== "data_center" && <label>Email Atlassian{effectiveCloud ? " · обязательно" : " · для Cloud"}<input type="text" inputMode="email" maxLength={320} required={effectiveCloud} pattern={effectiveCloud ? ".*\\S.*" : undefined} value={username} onChange={event => { connectionEdited.current = true; setManualCredentials(true); setUsername(event.target.value); }} autoComplete="username" /><small>{autoCloud ? "По адресу atlassian.net определён Confluence Cloud. Нужен email учётной записи, для которой выдан API token." : effectiveCloud ? "Укажите email учётной записи Atlassian, для которой выдан API token." : "Для Confluence Cloud нужны email и API token. Для Data Center достаточно персонального токена."}</small></label>}
        <label>Новый токен<input type="password" required value={token} onChange={event => { connectionEdited.current = true; setManualCredentials(true); setToken(event.target.value); }} autoComplete="new-password" /><small>Токен передаётся серверу и не возвращается в интерфейс.{source ? " Обновится только это подключение." : ""}</small></label>
        </>}
      </div><div className="modal-form">
        <label>Проверять обновления<select value={schedule.kind} onChange={event => setSchedule(current => ({ ...current, kind: event.target.value as Schedule["kind"] }))}><option value="manual">Только вручную</option><option value="interval">С интервалом</option><option value="daily">Ежедневно</option><option value="weekly">Еженедельно</option></select></label>
        {schedule.kind === "interval" && <label>Интервал, минут<input required type="number" min={1} max={525600} value={schedule.interval_minutes} onChange={event => setSchedule(current => ({ ...current, interval_minutes: Number(event.target.value) }))} /></label>}
        {schedule.kind === "weekly" && <label>День<select value={schedule.weekday} onChange={event => setSchedule(current => ({ ...current, weekday: Number(event.target.value) }))}>{["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"].map((day, i) => <option key={day} value={i}>{day}</option>)}</select></label>}
        {["daily", "weekly"].includes(schedule.kind) && <label>Время<input required type="time" value={schedule.time} onChange={event => setSchedule(current => ({ ...current, time: event.target.value }))} /></label>}
        {schedule.kind !== "manual" && <label>Часовой пояс<input required list="confluence-timezones" value={schedule.timezone} onChange={event => setSchedule(current => ({ ...current, timezone: event.target.value }))} placeholder="Europe/Moscow" /><datalist id="confluence-timezones"><option value="Europe/Moscow" /><option value="UTC" /><option value="Asia/Yekaterinburg" /><option value="Asia/Novosibirsk" /></datalist></label>}
        <p className="integration-muted">Загружается только актуальное содержимое страниц. При изменениях оно обновляется в выбранном индексе.</p>
        <div className="callout"><b>Распознавание изображений — только по кнопке</b><p>Синхронизация загружает страницы и вложения без запуска GigaCode. После загрузки можно отдельно нажать «Распознать изображения» в карточке источника.</p><small>Поддерживаются SVG, BMP, JPG, JPEG и PNG.</small>{gigacodeAvailable === false && <p className="integration-muted">GigaCode сейчас недоступен. Загрузка страниц продолжит работать.</p>}</div>
        <small>Синхронизация выполняется на сервере. Закрытие страницы её не останавливает.</small>
      </div></div></fieldset>
      {error && <div className="form-error" role="alert">{error}</div>}{check && <div className="access-notice" role="status">{check}</div>}
      <div className="modal-actions"><button type="button" className="button quiet" disabled={busy || checking} onClick={onClose}>Отмена</button><button type="button" className="button secondary" disabled={busy || checking} onClick={() => void test()}>{checking ? "Проверяем…" : "Проверить доступ"}</button><button className="button primary" disabled={busy || checking || !index}>{busy ? checking ? "Проверяем доступ…" : "Сохраняем…" : source ? "Сохранить" : "Подключить и синхронизировать"}</button></div>
    </form>
  </section></div>;
}

export default function ConfluenceApp({ password, indexes, indexId, onChanged }: { password: string; indexes: RagIndex[]; indexId?: string; onChanged?: () => void }) {
  const { request, login, revision } = useManagementApi(password);
  const [catalog, setCatalog] = useState<ConfluenceCatalog | null>(null);
  const [jobs, setJobs] = useState<ConfluenceJob[]>([]);
  const [editing, setEditing] = useState<ConfluenceSource | "new" | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [failuresJobId, setFailuresJobId] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState("");
  const [pagesSourceId, setPagesSourceId] = useState<string | null>(null);
  const [logRevisions, setLogRevisions] = useState<Record<string, number>>({});
  const [pendingActions, setPendingActions] = useState<Set<string>>(new Set());
  const pendingActionsRef = useRef(new Set<string>());
  const loadSequence = useRef(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(async (signal?: AbortSignal) => {
    const sequence = ++loadSequence.current;
    try {
      const params = new URLSearchParams(); if (indexId) params.set("index_id", indexId);
      const data = await request<ConfluenceCatalog>(`/admin/api/confluence?${params}`, { signal });
      if (signal?.aborted || sequence !== loadSequence.current) return;
      setCatalog(data); setLoading(false);
      if (data.enabled === false) { setJobs([]); setEditing(null); setLoadError(""); return; }
      const jobParams = new URLSearchParams({ limit: "30" }); if (sourceFilter) jobParams.set("source_id", sourceFilter); if (indexId) jobParams.set("index_id", indexId);
      const history = await request<{ jobs: ConfluenceJob[] }>(`/admin/api/confluence/jobs?${jobParams}`, { signal });
      if (signal?.aborted || sequence !== loadSequence.current) return;
      setJobs(history.jobs.filter(job => !indexId || job.index_id === indexId)); setLoadError("");
    } catch (caught) { if (!signal?.aborted && sequence === loadSequence.current) setLoadError(accessError(caught)); }
    finally { if (!signal?.aborted && sequence === loadSequence.current) setLoading(false); }
  }, [request, indexId, sourceFilter]);
  useEffect(() => {
    const controller = new AbortController(); let timer = 0;
    const poll = async () => { await load(controller.signal); if (!controller.signal.aborted) timer = window.setTimeout(poll, 4000); };
    void poll(); return () => { controller.abort(); window.clearTimeout(timer); };
  }, [load, revision]);
  const beginAction = (key: string) => {
    if (pendingActionsRef.current.has(key)) return false;
    pendingActionsRef.current.add(key); setPendingActions(new Set(pendingActionsRef.current));
    ++loadSequence.current;
    setError(""); setNotice("");
    return true;
  };
  const finishAction = (key: string) => {
    pendingActionsRef.current.delete(key); setPendingActions(new Set(pendingActionsRef.current));
  };
  const applyMutation = (result: { job?: ConfluenceJob; source?: ConfluenceSource }) => {
    // A response to an earlier poll must not overwrite the accepted operation.
    ++loadSequence.current;
    const { job, source: updatedSource } = result;
    if (job) setJobs(current => [job, ...current.filter(item => item.id !== job.id)]);
    setCatalog(current => current ? {
      ...current,
      jobs: job ? [job, ...(current.jobs ?? []).filter(item => item.id !== job.id)] : current.jobs,
      sources: current.sources.map(source => updatedSource?.id === source.id ? updatedSource : job?.source_id === source.id ? {
        ...source, active_job: active(job.status) ? job : null,
        status: active(job.status) ? job.status : source.paused || !source.enabled ? "paused" : source.last_error ? "error" : "idle",
      } : source),
    } : current);
  };
  const mutate = async (key: string, path: string, body: unknown, success: string) => {
    if (!beginAction(key)) return;
    try {
      const result = await request<{ job?: ConfluenceJob; source?: ConfluenceSource }>(path, { method: "POST", body: JSON.stringify(body) });
      applyMutation(result); setNotice(success);
      // Refresh in the background: a slow overview must not disable controls.
      void load(); onChanged?.();
    } catch (caught) { setError(accessError(caught)); }
    finally { finishAction(key); }
  };
  const cancelJob = (job: ConfluenceJob) => mutate(`cancel:${job.id}`, `/admin/api/confluence/jobs/${encodeURIComponent(job.id)}/cancel`, {}, "Отмена запрошена. Если выполняется сетевой запрос, источник освободится после его завершения.");
  const analyzeImages = async (source: ConfluenceSource) => {
    const key = `source:${source.id}`;
    if (!beginAction(key)) return;
    try {
      const result = await request<{ job: ConfluenceJob }>(`/admin/api/confluence/sources/${encodeURIComponent(source.id)}/images/analyze`, { method: "POST", body: JSON.stringify({}) });
      applyMutation(result);
      setSourceFilter(source.id); setExpanded(result.job.id);
      setNotice(`Распознавание изображений источника «${source.name}» поставлено в очередь. Ход обработки и ссылка для входа в GigaCode появятся в задаче ниже.`);
      void load(); onChanged?.();
    } catch (caught) { setError(accessError(caught)); }
    finally { finishAction(key); }
  };
  const clearLogs = async (source: ConfluenceSource) => {
    if (!window.confirm(`Очистить журналы всех синхронизаций источника «${source.name}»? Страницы, настройки, результаты задач и текущая синхронизация сохранятся. Новые записи продолжат появляться.`)) return;
    const key = `logs:${source.id}`;
    if (!beginAction(key)) return;
    try {
      const result = await request<{ deleted_events: number }>(`/admin/api/confluence/sources/${encodeURIComponent(source.id)}/logs/clear`, { method: "POST" });
      setLogRevisions(current => ({ ...current, [source.id]: (current[source.id] || 0) + 1 }));
      setNotice(`Журналы источника «${source.name}» очищены. Удалено записей: ${result.deleted_events}.`);
    } catch (caught) { setError(accessError(caught)); }
    finally { finishAction(key); }
  };
  const sources = (catalog?.sources ?? []).filter(source => !indexId || source.index_id === indexId);
  const activeSourceJob = (source: ConfluenceSource) => source.active_job !== undefined ? source.active_job : (catalog?.jobs ?? []).find(job => job.source_id === source.id && active(job.status));
  const currentJobs = sources.map(activeSourceJob).filter((job): job is ConfluenceJob => Boolean(job));
  const currentJobMap = new Map([...(catalog?.jobs ?? []), ...currentJobs].map(job => [job.id, job]));
  const authenticationJobs = currentJobs.filter(awaitingAuthentication);
  const authenticationSources = [...new Set(authenticationJobs.map(job => sources.find(source => source.id === job.source_id)?.name || job.source_id))];
  const visibleJobs = [...new Map([...jobs.map(job => currentJobMap.get(job.id) ?? job), ...currentJobs].map(job => [job.id, job])).values()].filter(job => (!indexId || job.index_id === indexId) && (!sourceFilter || job.source_id === sourceFilter) && (!active(job.status) || currentJobs.some(current => current.id === job.id)));
  const pagesSource = sources.find(source => source.id === pagesSourceId);
  const sourceBusy = (source: ConfluenceSource) => active(source.status) || Boolean(activeSourceJob(source));
  if (catalog?.enabled === false) return <div className="integration-area confluence-sources"><section className="panel integration-panel"><h3>Источники Confluence</h3><p className="integration-muted" role="status">Интеграция Confluence отключена на сервере.</p></section></div>;
  return <div className="integration-area confluence-sources">
    <section className="panel"><div className="panel-header"><div><span className="eyebrow">Документация по API</span><h3>Источники Confluence</h3></div>{catalog?.can_manage && <button className="button primary" onClick={() => setEditing("new")}>＋ Подключить Confluence</button>}</div>
      {(error || loadError) && <div className="form-error" role="alert">{error || loadError}</div>}{notice && <div className="access-notice" role="status">{notice}</div>}
      {authenticationJobs.length > 0 && <div className="confluence-authentication-summary" role="status"><div><strong>Требуется вход в GigaCode</strong><p>{authenticationSources.join(", ")}. {catalog?.can_manage ? "Ссылка для входа доступна в карточке задачи ниже." : "Войдите как администратор, чтобы пройти авторизацию."}</p></div>{sourceFilter && <button className="button secondary" onClick={() => setSourceFilter("")}>Показать все задачи</button>}</div>}
      {sources.length > 0 && <p className="confluence-source-intro integration-muted">Синхронизация обновляет страницы и вложения без запуска GigaCode. Распознавание изображений запускается только отдельной кнопкой и добавляет результат в индекс.</p>}
      {loading ? <div className="empty-state compact">Загружаем источники…</div> : sources.length ? <div className="confluence-source-list">{sources.map(source => {
        const currentJob = activeSourceJob(source);
        const status = currentJob?.status || source.status;
        const locked = sourceBusy(source);
        const sourcePending = pendingActions.has(`source:${source.id}`);
        const cancelling = currentJob?.status === "cancelling" || Boolean(currentJob && pendingActions.has(`cancel:${currentJob.id}`));
        const noPages = source.page_count === 0;
        const noExtractor = catalog?.gigacode?.available === false;
        return <article className="confluence-source-card" key={source.id} aria-labelledby={`confluence-source-${source.id}`}>
          <header className="confluence-source-header">
            <div className="confluence-source-identity">
              <h4 id={`confluence-source-${source.id}`}>{source.name}</h4>
              <a href={source.root_page_url} target="_blank" rel="noreferrer">Корневая страница ↗</a>
              <span className="integration-muted">{source.base_url}</span>
            </div>
            <div className="confluence-source-state"><span className="tag">{labels[status] || status}</span>{currentJob?.operation === "analyze_images" && <small>Распознавание изображений</small>}</div>
          </header>
          <dl className="confluence-source-facts">
            <div><dt>Индекс</dt><dd>{indexes.find(index => index.id === source.index_id)?.name || source.index_id}</dd></div>
            <div><dt>Загружено</dt><dd>{source.page_count} страниц <span>· {source.asset_count} вложений</span></dd></div>
            <div><dt>Распознавание изображений</dt><dd>Только по кнопке</dd></div>
          </dl>
          <dl className="confluence-source-checks">
            <div><dt>Последняя проверка</dt><dd>{integrationDate(source.last_checked_at)}</dd></div>
            <div><dt>Успешная проверка</dt><dd>{integrationDate(source.last_success_at)}</dd></div>
            <div><dt>Следующая проверка</dt><dd>{source.paused || !source.enabled ? "На паузе" : source.schedule.kind === "manual" ? "Только вручную" : integrationDate(source.next_check_at)}</dd></div>
          </dl>
          {source.last_error && <p className="confluence-source-error integration-error">{source.last_error}</p>}
          <div className="confluence-source-actions">
            {catalog?.can_manage && <div className="confluence-source-primary-actions">
              <button className="button secondary" disabled={sourcePending || locked} onClick={() => void mutate(`source:${source.id}`, `/admin/api/confluence/sources/${encodeURIComponent(source.id)}/sync`, {}, "Синхронизация поставлена в очередь")}>Синхронизировать</button>
              <button className="button secondary confluence-image-button" disabled={sourcePending || locked || noPages || noExtractor} onClick={() => void analyzeImages(source)} aria-describedby={`confluence-image-help-${source.id}`}>Распознать изображения</button>
              {currentJob && <button className="button secondary confluence-danger" disabled={cancelling} onClick={() => void cancelJob(currentJob)}>{cancelling ? "Отменяется…" : currentJob.status === "queued" ? "Убрать из очереди" : currentJob.operation === "analyze_images" ? "Остановить распознавание" : "Остановить загрузку"}</button>}
            </div>}
            <div className="confluence-source-view-actions">
              <button className="button quiet" aria-pressed={pagesSourceId === source.id} onClick={() => setPagesSourceId(source.id)}>Страницы</button>
              <button className="button quiet" aria-pressed={sourceFilter === source.id} onClick={() => setSourceFilter(source.id)}>Журнал</button>
            </div>
          </div>
          {catalog?.can_manage && <>
            <p id={`confluence-image-help-${source.id}`} className={`confluence-source-hint integration-muted${noExtractor ? " integration-error" : ""}`}>{cancelling ? "Отмена выполняется. Текущий сетевой запрос может завершиться перед остановкой. После отмены источник можно запустить снова." : noExtractor ? "GigaCode недоступен на сервере. Для распознавания нужно настроить его запуск." : locked ? "Дождитесь завершения задачи или остановите её кнопкой выше. Другие источники можно запускать и отменять независимо." : noPages ? "Сначала синхронизируйте источник, чтобы загрузить страницы для распознавания." : "Кнопка запускает одну задачу для новых и нераспознанных изображений. Автоматическое распознавание не включается; готовые результаты используются повторно."}</p>
            <details className="confluence-source-management">
              <summary>Настройки и управление</summary>
              <div className="confluence-source-management-actions">
                <div><button className="button quiet" disabled={sourcePending || locked} onClick={() => setEditing(source)}>Настроить</button><button className="button quiet" disabled={sourcePending} onClick={() => void mutate(`source:${source.id}`, `/admin/api/confluence/sources/${encodeURIComponent(source.id)}/pause`, { paused: !source.paused }, source.paused ? "Расписание возобновлено" : "Расписание приостановлено")}>{source.paused ? "Возобновить расписание" : "Приостановить расписание"}</button></div>
                <div><button className="button quiet" disabled={pendingActions.has(`logs:${source.id}`)} onClick={() => void clearLogs(source)}>Очистить журнал</button><button className="button quiet confluence-danger" disabled={sourcePending || locked} onClick={() => { if (window.confirm(`Отключить источник «${source.name}»? Сохранённые документы останутся в индексе.`)) void mutate(`source:${source.id}`, `/admin/api/confluence/sources/${encodeURIComponent(source.id)}/delete`, {}, "Источник отключён, документы сохранены"); }}>Отключить источник</button></div>
              </div>
            </details>
          </>}
        </article>;
      })}</div> : <div className="empty-state compact"><h3>Confluence ещё не подключён</h3><p>Выберите индекс, укажите сервер, корневую страницу и токен.</p></div>}
    </section>
    {pagesSource && <SourcePages key={pagesSource.id} source={pagesSource} request={request} onClose={() => setPagesSourceId(null)} />}
    {catalog && !catalog.can_manage && <ManagementLogin onLogin={login} />}
    {visibleJobs.length > 0 && <section className="panel integration-panel"><div className="integration-job-head"><h3>Задачи Confluence</h3><label>Источник<select value={sourceFilter} onChange={event => setSourceFilter(event.target.value)}><option value="">Все источники</option>{sources.map(source => <option key={source.id} value={source.id}>{source.name}</option>)}</select></label></div>{visibleJobs.map(job => {
      const imageJob = job.operation === "analyze_images";
      const progress = job.progress || {}; const count = (key: string) => typeof progress[key] === "number" ? Number(progress[key]) : null;
      const outcome = (key: string) => typeof job.result?.[key] === "number" ? Number(job.result[key]) : count(key);
      const failedItems = outcome("failed_items") ?? 0;
      const retryRound = count("retry_round") ?? 0;
      const processedChunks = count("completed") ?? count("processed") ?? count("processed_chunks");
      const processed = processedChunks ?? count("pages_done");
      const total = processedChunks !== null ? count("total") ?? count("total_chunks") : count("pages_total");
      return <article className="integration-job-row" key={job.id}>{imageJob && <div className="confluence-job-operation"><span className="tag">Распознавание изображений</span><p className="integration-muted">Обрабатываются изображения загруженных страниц. Описания будут добавлены в выбранный индекс.</p></div>}<div className="integration-job-head"><div><b>{stageLabels[job.phase] && ["retry_wait", "retry", "discovery_retry"].includes(job.phase) ? stageLabels[job.phase] : job.message || stageLabels[job.phase] || job.phase || "Синхронизация Confluence"}</b><small className="integration-muted"> · {labels[job.status] || job.status}</small><div className="integration-muted">{sources.find(source => source.id === job.source_id)?.name || job.source_id} · {indexes.find(index => index.id === job.index_id)?.name || job.index_id} · {integrationDate(job.completed_at || job.started_at || job.created_at)}</div></div><div className="integration-actions">{(failedItems > 0 || failuresJobId === job.id) && <button className="button secondary" aria-expanded={failuresJobId === job.id} onClick={() => setFailuresJobId(failuresJobId === job.id ? null : job.id)}>{failuresJobId === job.id ? "Скрыть ошибки" : `Ошибки загрузки (${failedItems})`}</button>}<button className="button quiet" onClick={() => setExpanded(expanded === job.id ? null : job.id)}>{expanded === job.id ? "Скрыть журнал" : "Журнал"}</button>{catalog?.can_manage && active(job.status) && <button className="button quiet" disabled={pendingActions.has(`cancel:${job.id}`) || job.status === "cancelling"} onClick={() => void cancelJob(job)}>{pendingActions.has(`cancel:${job.id}`) || job.status === "cancelling" ? "Отменяется…" : "Отменить"}</button>}</div></div>
        {awaitingAuthentication(job) && <GigaCodeAuthentication job={job} canManage={catalog?.can_manage === true} />}
        {total !== null && total > 0 && processed !== null && <progress className="integration-progress" value={processed} max={total} aria-label={processedChunks !== null ? "Обработанные фрагменты" : "Проверенные страницы"} />}
        <div className="integration-facts">{[[processedChunks !== null ? "Обработано фрагментов" : "Проверено страниц", processed !== null ? `${processed}${total !== null ? ` / ${total}` : ""}` : null], ["Из кэша", count("reused") ?? count("reused_chunks")], ["Рассчитано", count("computed") ?? count("computed_chunks")], ["Опубликовано", count("published") ?? count("published_chunks")]].map(([label, value]) => value !== null ? <span key={String(label)}>{label}<b>{value}</b></span> : null)}</div>
        <div className="integration-facts">{[["Повторный проход", active(job.status) && retryRound > 0 ? `${retryRound} / 2` : null], ["Ожидают повтора", active(job.status) && (count("retry_pending") ?? 0) > 0 ? count("retry_pending") : null], ["Восстановлено после ошибок", (outcome("recovered_pages") ?? 0) > 0 ? outcome("recovered_pages") : null], ["Страниц с ошибками", failedItems > 0 ? outcome("failed_pages") : null], ["Не прочитано веток", failedItems > 0 ? outcome("failed_branches") : null]].map(([label, value]) => value !== null ? <span key={String(label)}>{label}<b>{value}</b></span> : null)}</div>
        {!imageJob && job.result?.discovery_complete === false && <p className="confluence-partial-warning">Обход дерева не завершён: часть страниц могла остаться необнаруженной. Отсутствующие страницы не считаются удалёнными.</p>}
        {!active(job.status) && failedItems > 0 && <p className="confluence-partial-warning">Загрузка завершена не полностью. Успешно загруженные страницы сохранены; оставшиеся ошибки доступны в списке.</p>}
        {job.error && <div className="integration-error">{job.error}</div>}{failuresJobId === job.id && <ConfluenceFailures key={job.id} job={job} request={request} />}{expanded === job.id && <ConfluenceLog key={`${job.id}:${logRevisions[job.source_id] || 0}`} job={job} request={request} />}
      </article>;
    })}<p className="integration-muted">Задачи выполняются на сервере. Закрытие вкладки не прерывает обработку; журнал и сохранённый прогресс доступны после возвращения.</p></section>}
    {editing && <SourceForm source={editing === "new" ? null : editing} indexes={indexes} initialIndex={indexId} request={request} gigacodeAvailable={catalog?.gigacode?.available} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setNotice("Источник сохранён"); void load(); onChanged?.(); }} />}
  </div>;
}
