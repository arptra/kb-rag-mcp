import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { RegistrySkill, SkillRelease, SkillSource, SkillSourceInput, SkillSourcePreview, SkillVersion, SkillsConnection, SkillsInstall, SkillsJob } from "./skillsTypes";
import "./skills.css";

const BASE = "/admin/api/skills";
const TABS = [
  { id: "registry", label: "Реестр" },
  { id: "sources", label: "Источники" },
  { id: "jobs", label: "Синхронизация" },
  { id: "connect", label: "Подключение" },
] as const;
type Tab = typeof TABS[number]["id"];
type SkillsApi = {
  get: <T>(path: string, signal?: AbortSignal) => Promise<T>;
  post: <T>(path: string, body: unknown, signal?: AbortSignal) => Promise<T>;
  download: (path: string, filename: string) => Promise<void>;
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Не удалось выполнить запрос";
}

function createApi(password: string, secureMode: boolean): SkillsApi {
  const fetchResponse = async (path: string, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(path, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith(`${BASE}/`)) {
      throw new Error("Ссылка на пакет должна вести на этот сервер скиллов");
    }
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (init.signal?.aborted) abort();
    init.signal?.addEventListener("abort", abort, { once: true });
    const timeout = window.setTimeout(abort, 180000);
    const headers = new Headers(init.headers);
    if (init.body) headers.set("Content-Type", "application/json");
    if (password && !secureMode) headers.set("X-KB-Admin-Password", password);
    try {
      const adminMutation = ["/sources", "/sources/validate", "/sources/delete", "/sync", "/publish"].includes(url.pathname.slice(BASE.length));
      if (secureMode && init.method === "POST" && adminMutation) {
        const sessionResponse = await fetch("/access/api/session", { credentials: "same-origin", signal: controller.signal });
        if (!sessionResponse.ok) throw new Error("Для этого действия войдите как администратор на странице управления доступом.");
        const session = await sessionResponse.json() as { csrf_token?: string };
        if (!session.csrf_token) throw new Error("Не удалось получить токен административной сессии. Войдите повторно.");
        headers.set("X-CSRF-Token", session.csrf_token);
      }
      const response = await fetch(url, { ...init, credentials: "same-origin", headers, signal: controller.signal });
      if (!response.ok) {
        const text = await response.text();
        let detail = "";
        try { const parsed = JSON.parse(text) as { error?: string; detail?: string }; detail = parsed.error || parsed.detail || ""; } catch { /* Do not render a proxy HTML response. */ }
        if (response.status === 403) throw new Error(detail || "Недостаточно прав. Управление доступно администратору.");
        if (response.status === 401) throw new Error("Сессия истекла. Обновите страницу и войдите снова.");
        throw new Error(detail || `Ошибка сервера: ${response.status}`);
      }
      return response;
    } catch (caught) {
      if (controller.signal.aborted && !init.signal?.aborted) {
        throw new Error("Сервер не ответил за 180 секунд. Проверьте состояние операции в синхронизации.");
      }
      throw caught;
    } finally {
      window.clearTimeout(timeout);
      init.signal?.removeEventListener("abort", abort);
    }
  };
  return {
    get: async <T,>(path: string, signal?: AbortSignal) => (await fetchResponse(`${BASE}${path}`, { signal })).json() as Promise<T>,
    post: async <T,>(path: string, body: unknown, signal?: AbortSignal) => (await fetchResponse(`${BASE}${path}`, { method: "POST", body: JSON.stringify(body), signal })).json() as Promise<T>,
    download: async (path, filename) => {
      const response = await fetchResponse(path);
      const objectUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    },
  };
}

function useResource<T>(api: SkillsApi, path: string | null, revision = 0) {
  const [data, setData] = useState<T | null>(null);
  const [loadedPath, setLoadedPath] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(Boolean(path));
  useEffect(() => {
    const controller = new AbortController();
    if (!path) { setData(null); setLoading(false); setError(""); return; }
    setLoading(true);
    setError("");
    void api.get<T>(path, controller.signal).then((result) => {
      if (!controller.signal.aborted) { setData(result); setLoadedPath(path); }
    }).catch((caught) => {
      if (!controller.signal.aborted) { setError(message(caught)); setData(null); setLoadedPath(path); }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, path, revision]);
  return { data: loadedPath === path ? data : null, error: loadedPath === path ? error : "", loading: loading || Boolean(path && loadedPath !== path) };
}

function date(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

function hash(value?: string | null): string { return value ? value.slice(0, 12) : "—"; }
function bytes(value: number): string { return value < 1024 ? `${value} Б` : `${(value / 1024).toFixed(1)} КБ`; }
function Busy() { return <div className="skills-empty" role="status">Загрузка…</div>; }
function ErrorBox({ error }: { error: string }) { return error ? <div className="form-error" role="alert">{error}</div> : null; }
function Badge({ status, children }: { status: string; children?: React.ReactNode }) {
  const labels: Record<string, string> = { queued: "В очереди", running: "Выполняется", succeeded: "Готово", succeeded_with_warnings: "С предупреждениями", failed: "Ошибка", published: "Опубликован", candidate: "Новая версия", retired: "Нет в источнике", draft: "Не опубликован" };
  return <span className={`skills-badge skills-badge-${status}`}>{children || labels[status] || status}</span>;
}

const RETAINED_VERSIONS_NOTICE = "Скиллы с ошибками пропущены. Их прежние версии сохранены в реестре; новые версии этих скиллов не опубликованы.";

function SkillIssues({ issues = [], title, notice, expanded = false }: { issues?: unknown[]; title: string; notice?: string; expanded?: boolean }) {
  if (!issues.length) return null;
  return (
    <details className="skills-warnings" open={expanded}>
      <summary>{title} · {issues.length}</summary>
      {notice && <p>{notice}</p>}
      <ul>
        {issues.map((issue, index) => {
          const detail = typeof issue === "object" && issue !== null ? issue as Record<string, unknown> : null;
          const path = typeof detail?.path === "string" ? detail.path : typeof detail?.relative_path === "string" ? detail.relative_path : "";
          const reason = typeof issue === "string" ? issue : typeof detail?.error === "string" ? detail.error : typeof detail?.message === "string" ? detail.message : "Не удалось прочитать скилл. Проверьте его SKILL.md и файлы пакета.";
          return <li key={`${path}:${index}`}>{path && <code>{path}</code>}<p>{reason}</p></li>;
        })}
      </ul>
    </details>
  );
}

export default function SkillsApp({ password, secureMode }: { password: string; secureMode: boolean }) {
  const api = useMemo(() => createApi(password, secureMode), [password, secureMode]);
  const [tab, setTab] = useState<Tab>("registry");
  const [revision, setRevision] = useState(0);
  const [jobRevision, setJobRevision] = useState(0);
  const [selected, setSelected] = useState<Record<string, RegistrySkill>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const status = useResource<{ can_manage: boolean; mcp_path: string }>(api, "/status", revision);
  const sources = useResource<{ sources: SkillSource[] }>(api, "/sources", revision);
  const jobs = useResource<{ jobs: SkillsJob[] }>(api, "/jobs", jobRevision);
  const activeJobs = jobs.data?.jobs.some((job) => ["queued", "running"].includes(job.status)) ?? false;
  const lastJobState = useRef("");
  const jobState = jobs.data?.jobs.map((job) => `${job.id}:${job.status}`).join("|") ?? "";
  useEffect(() => {
    if (lastJobState.current && lastJobState.current !== jobState && jobState) setRevision((value) => value + 1);
    if (jobState) lastJobState.current = jobState;
  }, [jobState]);
  useEffect(() => {
    if ((!activeJobs && tab !== "jobs") || jobs.loading) return;
    const timer = window.setTimeout(() => setJobRevision((value) => value + 1), 4000);
    return () => window.clearTimeout(timer);
  }, [activeJobs, tab, jobs.loading, jobRevision]);
  const changed = useCallback(() => { setRevision((value) => value + 1); setJobRevision((value) => value + 1); }, []);
  useEffect(() => {
    window.addEventListener("focus", changed);
    return () => window.removeEventListener("focus", changed);
  }, [changed]);
  const sync = async (sourceId?: string) => {
    setBusy(true); setError(""); setNotice("");
    try {
      await api.post("/sync", sourceId ? { source_id: sourceId } : {});
      changed(); setNotice("Проверка поставлена в очередь. Результат появится в синхронизации.");
    } catch (caught) { setError(message(caught)); } finally { setBusy(false); }
  };
  const choose = (skill: RegistrySkill, checked: boolean) => setSelected((previous) => {
    const next = { ...previous };
    if (checked) next[skill.skill_id] = skill; else delete next[skill.skill_id];
    return next;
  });
  const canManage = status.data?.can_manage === true;
  return (
    <div className="skills-app">
      <section className="skills-intro">
        <div><span className="eyebrow">Корпоративные инструкции</span><h2>Реестр скиллов</h2><p>Версии из Git, публикация и установка через отдельный MCP.</p></div>
        <div className="skills-actions"><button className="button" onClick={changed}>↻ Обновить данные</button>{canManage && <button className="button primary" disabled={busy || !sources.data?.sources.some((source) => source.enabled && !source.archived)} onClick={() => void sync()}>Проверить все</button>}</div>
      </section>
      <nav className="skills-tabs" aria-label="Разделы скиллов">
        {TABS.map((item) => <button key={item.id} className={tab === item.id ? "active" : ""} aria-current={tab === item.id ? "page" : undefined} onClick={() => { setTab(item.id); setError(""); setNotice(""); }}>{item.label}{item.id === "connect" && Object.keys(selected).length > 0 && <span>{Object.keys(selected).length}</span>}{item.id === "jobs" && activeJobs && <i aria-label="Есть активные проверки" />}</button>)}
      </nav>
      <ErrorBox error={status.error || error} />
      {notice && <div className="skills-notice" role="status">{notice}</div>}
      {status.data && !canManage && <div className="skills-hint">Доступен просмотр и скачивание скиллов. Для настройки источников и публикации <a href="/access-admin">войдите как администратор</a>.</div>}
      {tab === "registry" && <Registry api={api} revision={revision} sources={sources.data?.sources || []} canManage={canManage} selected={selected} onChoose={choose} onChanged={changed} onConnect={() => setTab("connect")} />}
      {tab === "sources" && <Sources api={api} sources={sources.data?.sources || []} loading={sources.loading} error={sources.error} canManage={canManage} busy={busy} onChanged={changed} onSync={sync} />}
      {tab === "jobs" && <Jobs jobs={jobs.data?.jobs || []} sources={sources.data?.sources || []} error={jobs.error} loading={jobs.loading && !jobs.data} onRefresh={() => setJobRevision((value) => value + 1)} />}
      {tab === "connect" && <Connection api={api} selected={selected} onRemove={(skill) => choose(skill, false)} onRegistry={() => setTab("registry")} />}
    </div>
  );
}

function Registry({ api, revision, sources, canManage, selected, onChoose, onChanged, onConnect }: {
  api: SkillsApi; revision: number; sources: SkillSource[]; canManage: boolean; selected: Record<string, RegistrySkill>;
  onChoose: (skill: RegistrySkill, checked: boolean) => void; onChanged: () => void; onConnect: () => void;
}) {
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [source, setSource] = useState("");
  const [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState<string | null>(null);
  useEffect(() => { const timer = window.setTimeout(() => { setDebouncedQuery(query); setOffset(0); }, 250); return () => window.clearTimeout(timer); }, [query]);
  const params = new URLSearchParams({ query: debouncedQuery, offset: String(offset), limit: "25" });
  if (source) params.set("source_id", source);
  const registry = useResource<{ skills: RegistrySkill[]; total: number }>(api, `/registry?${params}`, revision);
  if (detail) return <SkillDetail key={detail} api={api} skillId={detail} revision={revision} canManage={canManage} onBack={() => setDetail(null)} onChanged={onChanged} />;
  return <>
    <div className="skills-toolbar">
      <input aria-label="Поиск скиллов" type="search" maxLength={500} placeholder="Название или описание скилла" value={query} onChange={(event) => setQuery(event.target.value)} />
      <select aria-label="Источник скиллов" value={source} onChange={(event) => { setSource(event.target.value); setOffset(0); }}><option value="">Все источники</option>{sources.map((item) => <option key={item.id} value={item.id}>{item.name}{item.archived ? " · архив" : ""}</option>)}</select>
      {Object.keys(selected).length > 0 && <button className="button primary" onClick={onConnect}>Установить выбранные · {Object.keys(selected).length}</button>}
    </div>
    <ErrorBox error={registry.error} />
    {registry.loading ? <Busy /> : !registry.data?.skills.length ? <div className="skills-empty"><b>Скиллы не найдены</b><p>{query || source ? "Попробуйте другой запрос или источник." : "Добавьте Git-источник и запустите проверку во вкладке «Источники»."}</p></div> : <>
      <div className="skills-table-wrap"><table className="skills-table"><thead><tr><th><span className="skills-sr-only">Выбор для установки</span></th><th>Скилл</th><th>Источник</th><th>Публикация</th><th>Последняя версия</th><th /></tr></thead><tbody>
        {registry.data.skills.map((skill) => <tr key={skill.skill_id}>
          <td><input type="checkbox" aria-label={`Выбрать ${skill.name}`} disabled={!selected[skill.skill_id] && (!skill.published_revision || skill.retired || Object.keys(selected).length >= 32)} checked={Boolean(selected[skill.skill_id])} onChange={(event) => onChoose(skill, event.target.checked)} /></td>
          <td><button className="skills-title-button" onClick={() => setDetail(skill.skill_id)}>{skill.name}</button><small className="skills-description">{skill.description || "Без описания"}</small></td>
          <td><span>{sources.find((item) => item.id === skill.source_id)?.name || skill.source_name || skill.source_id}</span><small><code>{skill.path}</code></small></td>
          <td><Badge status={skill.retired ? "retired" : !skill.published_revision ? "draft" : skill.published_revision !== skill.latest_revision ? "candidate" : "published"} /><small>{skill.published_revision ? <>v{skill.published_version || "?"} · <code title={skill.published_revision}>{hash(skill.published_revision)}</code></> : "—"}</small></td>
          <td><span>v{skill.latest_version || skill.version || "?"}</span><small><code title={skill.latest_revision || ""}>{hash(skill.latest_revision)}</code></small></td>
          <td><button className="button quiet" onClick={() => setDetail(skill.skill_id)}>Открыть →</button></td>
        </tr>)}
      </tbody></table></div>
      <div className="skills-pagination"><span>{offset + 1}–{Math.min(offset + 25, registry.data.total)} из {registry.data.total}</span><button className="button" disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - 25))}>Назад</button><button className="button" disabled={offset + 25 >= registry.data.total} onClick={() => setOffset((value) => value + 25)}>Далее</button></div>
    </>}
  </>;
}

function SkillDetail({ api, skillId, revision, canManage, onBack, onChanged }: { api: SkillsApi; skillId: string; revision: number; canManage: boolean; onBack: () => void; onChanged: () => void }) {
  const detail = useResource<{ skill: RegistrySkill; versions: SkillVersion[] }>(api, `/detail?skill_id=${encodeURIComponent(skillId)}`, revision);
  const [chosenRevision, setChosenRevision] = useState("");
  const [path, setPath] = useState("SKILL.md");
  const [compare, setCompare] = useState("");
  const [showDiff, setShowDiff] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const skill = detail.data?.skill;
  const current = chosenRevision || skill?.published_revision || skill?.latest_revision || "";
  const params = new URLSearchParams({ skill_id: skillId, revision: current });
  const release = useResource<SkillRelease>(api, current ? `/release?${params}` : null);
  const filePath = release.data?.files.some((file) => file.path === path) ? path : release.data?.files[0]?.path || "";
  const file = useResource<{ encoding: string; content: string; path: string; sha256: string; size: number }>(api, current && filePath && !release.loading && !showDiff ? `/file?${params}&path=${encodeURIComponent(filePath)}` : null);
  const compareRevision = compare && compare !== current && detail.data?.versions.some((version) => version.revision === compare)
    ? compare : detail.data?.versions.find((version) => version.revision !== current)?.revision || "";
  const diffParams = new URLSearchParams({ skill_id: skillId, from_revision: compareRevision, to_revision: current });
  const diff = useResource<{ files: Array<{ path: string; status: string; diff?: string; truncated?: boolean }> }>(api, showDiff && current && compareRevision ? `/diff?${diffParams}` : null);
  const publish = async () => {
    setBusy(true); setError("");
    try { await api.post("/publish", { skill_id: skillId, revision: current }); onChanged(); }
    catch (caught) { setError(message(caught)); } finally { setBusy(false); }
  };
  return <div className="skills-detail">
    <button className="button quiet" onClick={onBack}>← К реестру</button>
    <ErrorBox error={detail.error || error} />
    {detail.loading && !detail.data ? <Busy /> : skill && <>
      <header className="skills-detail-heading"><div><h3>{skill.name}</h3><p>{skill.description}</p><code>{skillId}</code></div><Badge status={skill.retired ? "retired" : skill.published_revision ? "published" : "draft"} /></header>
      {skill.retired && <div className="skills-hint">Скилл больше не найден в источнике. Сохранённые версии доступны для просмотра; с клиентских компьютеров ничего не удаляется.</div>}
      <div className="skills-version-bar">
        <label>Версия<select value={current} onChange={(event) => { setChosenRevision(event.target.value); setPath("SKILL.md"); }}>{detail.data?.versions.map((version) => <option key={version.revision} value={version.revision}>v{version.version} · {hash(version.revision)}{version.revision === skill.published_revision ? " · опубликована" : ""}</option>)}</select></label>
        <div className="skills-version-meta"><small>Опубликованная ревизия</small><code>{hash(skill.published_revision)}</code></div>
        {canManage && current !== skill.published_revision && <button className="button primary" disabled={busy || !current || skill.retired} onClick={() => void publish()}>{busy ? "Публикуем…" : current === skill.latest_revision ? "Опубликовать версию" : "Откатить публикацию на эту версию"}</button>}
      </div>
      <div className="skills-subtabs"><button className={!showDiff ? "active" : ""} onClick={() => setShowDiff(false)}>Файлы</button><button className={showDiff ? "active" : ""} onClick={() => setShowDiff(true)}>Изменения версий</button></div>
      {showDiff ? <>
        <div className="skills-version-bar"><label>Сравнить выбранную версию с<select value={compareRevision} onChange={(event) => setCompare(event.target.value)}>{detail.data?.versions.filter((version) => version.revision !== current).map((version) => <option key={version.revision} value={version.revision}>v{version.version} · {hash(version.revision)}</option>)}</select></label><small>Изменения: {hash(compareRevision)} → {hash(current)}</small></div>
        <ErrorBox error={diff.error} />
        {!compareRevision ? <div className="skills-empty">Для сравнения нужны две версии.</div> : diff.loading ? <Busy /> : diff.data?.files.length ? <div className="skills-diffs">{diff.data.files.map((entry) => <section key={entry.path}><header><code>{entry.path}</code><span>{({ added: "Добавлен", removed: "Удалён", modified: "Изменён", unchanged: "Без изменений" } as Record<string, string>)[entry.status] || entry.status}</span></header>{entry.diff ? <pre>{entry.diff}</pre> : <p>Текстовое сравнение недоступно.</p>}{entry.truncated && <p>Сравнение сокращено из-за размера. Откройте файлы выбранных версий отдельно.</p>}</section>)}</div> : <div className="skills-empty">Изменений нет.</div>}
      </> : <>
        <ErrorBox error={release.error || file.error} />
        {release.loading ? <Busy /> : release.data && <>
          <div className="skills-release-meta"><span>Создана {date(release.data.created_at)}</span><span>Git commit <code title={release.data.commit || release.data.git_commit}>{hash(release.data.commit || release.data.git_commit)}</code></span><span>{release.data.files.length} файлов</span></div>
          <div className="skills-file-view"><nav aria-label="Файлы скилла">{release.data.files.map((entry) => <button key={entry.path} className={entry.path === filePath ? "active" : ""} onClick={() => setPath(entry.path)}><code>{entry.path}</code><small>{bytes(entry.size)}</small></button>)}</nav><section><header><code>{filePath}</code><small>{file.data && <span title={file.data.sha256}>SHA-256 {hash(file.data.sha256)}</span>}</small></header>{file.loading ? <Busy /> : file.data?.encoding === "base64" ? <div className="skills-empty">Двоичный файл · {bytes(file.data.size)}. Включён в пакет установки.</div> : <pre>{file.data?.content || ""}</pre>}</section></div>
        </>}
      </>}
      <p className="skills-footnote">Публикация меняет версию, доступную клиентам. Установка и загрузка скилла в CLI выполняются на компьютере пользователя.</p>
    </>}
  </div>;
}

const EMPTY_SOURCE: SkillSourceInput = { name: "", git_url: "", ref: "HEAD", skills_path: "skills", recursive: true, enabled: true, interval_minutes: 30, auto_publish: true };

function Sources({ api, sources, loading, error, canManage, busy, onChanged, onSync }: { api: SkillsApi; sources: SkillSource[]; loading: boolean; error: string; canManage: boolean; busy: boolean; onChanged: () => void; onSync: (sourceId?: string) => Promise<void> }) {
  const [editing, setEditing] = useState<SkillSourceInput | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const visibleSources = sources.filter((source) => showArchived || !source.archived);
  const remove = async (id: string) => {
    setDeleteBusy(true); setDeleteError("");
    try { await api.post("/sources/delete", { source_id: id }); setDeleting(null); onChanged(); }
    catch (caught) { setDeleteError(message(caught)); } finally { setDeleteBusy(false); }
  };
  return <>
    <div className="skills-section-head"><div><h3>Git-источники</h3><p>Можно подключить несколько репозиториев или разные каталоги одной репы.</p></div>{canManage && !editing && <button className="button primary" onClick={() => setEditing({ ...EMPTY_SOURCE })}>＋ Добавить источник</button>}</div>
    {sources.some((source) => source.archived) && <label className="skills-archive-toggle"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />Показать архивные источники</label>}
    {editing && <SourceForm key={editing.id || "new"} api={api} initial={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); onChanged(); }} />}
    <ErrorBox error={error || deleteError} />
    {loading && !sources.length ? <Busy /> : !visibleSources.length ? <div className="skills-empty"><b>Нет подключённых источников</b><p>Укажите Git URL, ветку и каталог со скиллами. Предварительная проверка покажет найденные пакеты.</p></div> : <div className="skills-source-grid">{visibleSources.map((source) => <article className="skills-source-card" key={source.id}>
      <header>
        <h3>{source.name}</h3>
        <div className="skills-source-badges">
          <Badge status={source.enabled && !source.archived ? "published" : "draft"}>{source.archived ? "В архиве" : source.enabled ? "Включён" : "Отключён"}</Badge>
          {Boolean(source.last_warnings?.length) && <Badge status="succeeded_with_warnings" />}
        </div>
      </header>
      <code className="skills-source-url">{source.git_url}</code>
      <dl><div><dt>Ветка / тег / commit</dt><dd><code>{source.ref}</code></dd></div><div><dt>Каталог</dt><dd><code>{source.skills_path || "."}</code>{source.recursive && " · с подкаталогами"}</dd></div><div><dt>Проверка</dt><dd>{source.interval_minutes > 0 ? `Каждые ${source.interval_minutes} мин` : "Только вручную"}</dd></div><div><dt>Публикация</dt><dd>{source.auto_publish ? "Автоматически после проверки" : "Вручную"}</dd></div><div><dt>Последняя попытка</dt><dd>{date(source.last_checked_at)}</dd></div><div><dt>Успешная проверка</dt><dd>{date(source.last_success_at)}</dd></div><div><dt>Следующая проверка</dt><dd>{source.enabled && source.interval_minutes > 0 ? date(source.next_check_at) : "—"}</dd></div></dl>
      {source.last_error && <div className="form-error">{source.last_error}</div>}
      <SkillIssues
        issues={source.last_warnings}
        title={source.last_error ? "Предупреждения предыдущей проверки" : "Пропущены при последней проверке"}
        notice={source.last_error ? `Последняя попытка завершилась ошибкой. Эти предупреждения относятся к предыдущему обработанному состоянию источника. ${RETAINED_VERSIONS_NOTICE}` : RETAINED_VERSIONS_NOTICE}
      />
      {canManage && <footer className="skills-actions">{!source.archived && <button className="button" disabled={busy} onClick={() => void onSync(source.id)}>Проверить сейчас</button>}<button className="button quiet" onClick={() => { setEditing({ ...source }); setDeleting(null); }}>{source.archived ? "Восстановить и настроить" : "Настроить"}</button>{!source.archived && <button className="button quiet skills-danger" disabled={deleteBusy} onClick={() => setDeleting(source.id)}>Удалить</button>}</footer>}
      {deleting === source.id && <div className="skills-delete-confirm"><p>Удалить источник «{source.name}»? Его автоматические проверки остановятся; сохранённые версии останутся в истории.</p><div className="skills-actions"><button className="button skills-danger" disabled={deleteBusy} onClick={() => void remove(source.id)}>Удалить источник</button><button className="button quiet" disabled={deleteBusy} onClick={() => setDeleting(null)}>Отмена</button></div></div>}
    </article>)}</div>}
  </>;
}

function SourceForm({ api, initial, onClose, onSaved }: { api: SkillsApi; initial: SkillSourceInput; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState(initial);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"validate" | "save" | null>(null);
  const [preview, setPreview] = useState<SkillSourcePreview | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const update = <K extends keyof SkillSourceInput>(key: K, value: SkillSourceInput[K]) => { setForm((current) => ({ ...current, [key]: value })); setPreview(null); };
  const submit = async (operation: "validate" | "save") => {
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    setBusy(operation); setError("");
    const payload: SkillSourceInput = {
      ...(form.id ? { id: form.id } : {}), name: form.name, git_url: form.git_url,
      ref: form.ref, skills_path: form.skills_path, recursive: form.recursive,
      enabled: form.enabled, interval_minutes: form.interval_minutes, auto_publish: form.auto_publish,
    };
    try {
      if (operation === "validate") setPreview(await api.post("/sources/validate", payload, request.signal));
      else { await api.post("/sources", payload, request.signal); if (!request.signal.aborted) onSaved(); }
    } catch (caught) { if (!request.signal.aborted) setError(message(caught)); }
    finally { if (!request.signal.aborted) setBusy(null); }
  };
  const onSubmit = (event: FormEvent) => { event.preventDefault(); void submit("save"); };
  return <form className="skills-source-form" onSubmit={onSubmit}>
    <header><h3>{form.id ? "Настройка источника" : "Новый источник"}</h3><button type="button" className="button quiet" onClick={onClose}>Закрыть</button></header>
    <fieldset disabled={Boolean(busy)}><div className="skills-form-grid">
      <label>Название<input required maxLength={120} value={form.name} placeholder="Командные скиллы" onChange={(event) => update("name", event.target.value)} /></label>
      <label>Git URL<input required maxLength={2048} value={form.git_url} placeholder="https://git.example.ru/team/skills.git" onChange={(event) => update("git_url", event.target.value)} /><small>Для закрытых репозиториев используется настроенный на сервере Git-доступ. Не вставляйте токен в URL.</small></label>
      <label>Ветка, тег или commit<input required maxLength={256} value={form.ref} placeholder="HEAD" onChange={(event) => update("ref", event.target.value)} /></label>
      <label>Путь к скиллам в репозитории<input value={form.skills_path} placeholder="skills" onChange={(event) => update("skills_path", event.target.value)} /><small>Например, skills или .gigacode/skills; точка — корень репозитория.</small></label>
      <label>Интервал проверки, минуты<input required type="number" min={0} max={525600} step={1} value={form.interval_minutes} onChange={(event) => update("interval_minutes", Number(event.target.value))} /><small>0 — запускать только кнопкой. Интервал отсчитывается сервером.</small></label>
      <div className="skills-checks"><label><input type="checkbox" checked={form.enabled} onChange={(event) => update("enabled", event.target.checked)} />Источник включён</label><label><input type="checkbox" checked={form.recursive} onChange={(event) => update("recursive", event.target.checked)} />Искать в подкаталогах</label><label><input type="checkbox" checked={form.auto_publish} onChange={(event) => update("auto_publish", event.target.checked)} />Публиковать прошедшие проверку версии автоматически</label></div>
    </div></fieldset>
    <ErrorBox error={error} />
    {preview && <section className="skills-validation">
      <header>
        <b>Результат проверки источника</b>
        {Boolean(preview.warnings?.length) && <Badge status="succeeded_with_warnings" />}
      </header>
      <div className="skills-job-results">
        <span>Найдено <b>{preview.discovered ?? preview.skills.length}</b></span>
        <span>Прошло проверку <b>{preview.skills.length}</b></span>
        <span>Пропущено <b>{preview.skipped ?? preview.warnings?.length ?? 0}</b></span>
      </div>
      <SkillIssues
        issues={preview.warnings}
        title="Скиллы с ошибками"
        notice="При синхронизации эти скиллы будут пропущены; их ранее сохранённые версии останутся в реестре."
        expanded
      />
      <SkillIssues issues={preview.errors} title="Ошибки проверки" expanded />
      {preview.skills.map((skill) => <div className="skills-validation-skill" key={skill.relative_path}><strong>{skill.name}</strong><code>{skill.relative_path}</code><p>{skill.description}</p></div>)}
      {!preview.skills.length && <p>{preview.warnings?.length ? "Ни один скилл не прошёл проверку. Исправьте перечисленные ошибки и проверьте источник снова." : "Проверьте путь и наличие файлов SKILL.md."}</p>}
    </section>}
    <footer className="skills-actions"><button type="button" className="button" disabled={Boolean(busy) || !form.git_url.trim()} onClick={() => void submit("validate")}>{busy === "validate" ? "Проверяем Git…" : "Проверить источник"}</button><button className="button primary" type="submit" disabled={Boolean(busy)}>{busy === "save" ? "Сохраняем…" : "Сохранить источник"}</button></footer>
  </form>;
}

function Jobs({ jobs, sources, error, loading, onRefresh }: { jobs: SkillsJob[]; sources: SkillSource[]; error: string; loading: boolean; onRefresh: () => void }) {
  return <>
    <div className="skills-section-head"><div><h3>Проверки и обновления</h3><p>Ручные и автоматические проверки используют общую очередь. Эта вкладка обновляется каждые 4 секунды.</p></div><button className="button" onClick={onRefresh}>Обновить журнал</button></div>
    <ErrorBox error={error} />
    {loading ? <Busy /> : !jobs.length ? <div className="skills-empty">Проверок ещё не было. Добавьте источник и нажмите «Проверить сейчас».</div> : <div className="skills-job-list">{jobs.map((job) => <article key={job.id} className="skills-job">
      <header>
        <div><b>{job.source_name || sources.find((source) => source.id === job.source_id)?.name || job.source_id}</b><small>{date(job.created_at)} · {job.started_at ? `начало ${date(job.started_at)}` : "ожидает запуска"}{job.finished_at ? ` · завершение ${date(job.finished_at)}` : ""}</small></div>
        <Badge status={job.status} />
      </header>
      {job.result && <>
        <div className="skills-job-results">
          <span>Найдено <b>{job.result.discovered ?? 0}</b></span>
          <span>Прошло проверку <b>{job.result.valid ?? job.result.discovered ?? 0}</b></span>
          <span>Пропущено <b>{job.result.skipped ?? job.result.warnings?.length ?? 0}</b></span>
          <span>Новых версий <b>{job.result.created ?? 0}</b></span>
          <span>Опубликовано <b>{job.result.published ?? 0}</b></span>
          <span>Исчезло из Git <b>{job.result.retired ?? 0}</b></span>
        </div>
        <SkillIssues issues={job.result.warnings} title="Пропущенные скиллы" notice={RETAINED_VERSIONS_NOTICE} />
      </>}
      {job.error && <div className="form-error">{job.error}</div>}
    </article>)}</div>}
  </>;
}

function CopyBlock({ title, value }: { title: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const copy = async () => { try { await navigator.clipboard.writeText(value); setCopied(true); setError(""); } catch { setError("Буфер обмена недоступен. Выделите и скопируйте текст вручную."); } };
  useEffect(() => { if (!copied) return; const timer = window.setTimeout(() => setCopied(false), 2500); return () => window.clearTimeout(timer); }, [copied]);
  return <section className="skills-copy-block"><header><h4>{title}</h4><button className="button" onClick={() => void copy()}>{copied ? "Скопировано" : "Скопировать"}</button></header><pre tabIndex={0}>{value}</pre><ErrorBox error={error} /></section>;
}

function Connection({ api, selected, onRemove, onRegistry }: { api: SkillsApi; selected: Record<string, RegistrySkill>; onRemove: (skill: RegistrySkill) => void; onRegistry: () => void }) {
  const connection = useResource<SkillsConnection>(api, "/connect");
  const [scope, setScope] = useState<"user" | "project">("user");
  const [install, setInstall] = useState<SkillsInstall | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const selectedSkills = Object.values(selected);
  const selectionKey = selectedSkills.map((skill) => `${skill.skill_id}:${skill.published_revision}`).sort().join("|");
  useEffect(() => { setInstall(null); controller.current?.abort(); setBusy(false); return () => controller.current?.abort(); }, [selectionKey, scope]);
  const prepare = async () => {
    const request = new AbortController(); controller.current?.abort(); controller.current = request;
    setBusy(true); setError("");
    try {
      const result = await api.post<SkillsInstall>("/prepare-install", { skills: selectedSkills.map((skill) => ({ skill_id: skill.skill_id, revision: skill.published_revision })), scope }, request.signal);
      if (!request.signal.aborted) setInstall(result);
    } catch (caught) { if (!request.signal.aborted) setError(message(caught)); }
    finally { if (!request.signal.aborted) setBusy(false); }
  };
  const download = async (url: string, filename: string) => { setBusy(true); setError(""); try { await api.download(url, filename); } catch (caught) { setError(message(caught)); } finally { setBusy(false); } };
  return <>
    <div className="skills-section-head"><div><h3>Подключение GigaCode</h3><p>Штатные MCP, скиллы и расширения CLI. Дополнительный скрипт запуска не нужен.</p></div></div>
    <ErrorBox error={connection.error || error} />
    {connection.loading ? <Busy /> : connection.data && <>
      <section className="skills-connect-step"><span className="skills-step-number">1</span><div><h3>Подключите сервер скиллов</h3><p>Добавьте этот MCP в настройки CLI. Используйте выданные вам параметры авторизации.</p><div className="skills-endpoint"><code>{connection.data.mcp_url}</code></div><CopyBlock title="Конфигурация MCP" value={typeof connection.data.mcp_config === "string" ? connection.data.mcp_config : JSON.stringify(connection.data.mcp_config, null, 2)} /></div></section>
      <section className="skills-connect-step"><span className="skills-step-number">2</span><div><h3>Выберите скиллы для установки</h3><p>Пакет закрепляет конкретные опубликованные версии. До 32 скиллов в одном пакете; установка для пользователя или текущего проекта.</p>
        <div className="skills-selection">{selectedSkills.length ? selectedSkills.map((skill) => <div key={skill.skill_id}><b>{skill.name}</b><code>{hash(skill.published_revision)}</code><button className="button quiet" onClick={() => onRemove(skill)} aria-label={`Убрать ${skill.name}`}>×</button></div>) : <p>Отметьте нужные скиллы в реестре.</p>}</div>
        <div className="skills-actions"><button className="button" onClick={onRegistry}>Выбрать в реестре</button><label className="skills-scope">Установка<select value={scope} onChange={(event) => setScope(event.target.value as "user" | "project")}><option value="user">Для пользователя</option><option value="project">В текущий проект</option></select></label><button className="button primary" disabled={busy || !selectedSkills.length} onClick={() => void prepare()}>{busy ? "Подготовка…" : "Подготовить установку"}</button></div>
        {install && <><CopyBlock title="Промпт установки выбранных версий" value={install.bootstrap_prompt} /><div className="skills-actions">{install.download_url && <button className="button" disabled={busy} onClick={() => void download(install.download_url!, "corporate-skills-extension.zip")}>Скачать расширение ZIP</button>}</div>{install.download_url && <p className="skills-footnote">Поддержку установки архива нужно проверить в вашей сборке командой <code>gigacode extensions install --help</code>. Промпт выше также позволяет установить скиллы штатными файловыми инструментами CLI.</p>}<details className="skills-manifest"><summary>Состав пакета и контрольные суммы</summary><pre>{JSON.stringify(install.manifest, null, 2)}</pre></details></>}
      </div></section>
      <section className="skills-connect-step"><span className="skills-step-number">3</span><div><h3>Запустите установку в CLI</h3><p>После подключения MCP передайте GigaCode промпт ниже или промпт выбранного набора. Агент запросит инструкции и установит файлы штатными инструментами CLI, соблюдая её разрешения.</p><CopyBlock title="Промпт установки и обновления" value={connection.data.bootstrap_prompt} /><div className="skills-actions"><button className="button" disabled={busy} onClick={() => void download(`${BASE}/client-skill`, "SKILL.md")}>Скачать клиентский SKILL.md</button>{connection.data.prompt_name && <small>MCP prompt: <code>{connection.data.prompt_name}</code></small>}</div><div className="skills-hint">После установки или обновления перезапустите GigaCode, чтобы загрузить скиллы. Автообновление расширений в вашей сборке GigaCode пока не подтверждено: скачивание ZIP и публикация версии на сервере сами по себе не обновляют клиента.</div></div></section>
    </>}
  </>;
}
