import {
  FormEvent,
  lazy,
  ReactNode,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { ApiError, api, download, post } from "./api";
import type {
  CatalogJob,
  GraphNode,
  GraphOverview,
  GraphPayload,
  IndexDocument,
  IndexDocumentDetail,
  IndexDocumentsPage,
  ManagedTool,
  McpServer,
  Overview,
  Page,
  RagIndex,
  RepositorySource,
  ToolCatalogItem,
} from "./types";

const NAV: Array<{ id: Page; label: string; mark: string }> = [
  { id: "overview", label: "Обзор", mark: "◫" },
  { id: "indexes", label: "Индексы", mark: "◇" },
  { id: "services", label: "Сервисы", mark: "▦" },
  { id: "servers", label: "MCP servers", mark: "◉" },
  { id: "tools", label: "MCP tools", mark: "⌁" },
  { id: "graph", label: "Граф системы", mark: "⌘" },
  { id: "operations", label: "Операции и логи", mark: "≡" },
];

const TOOL_SCHEMA = {
  type: "object",
  properties: {
    query: { type: "string", description: "Question for the selected knowledge indexes" },
    top_k: { type: "integer", minimum: 1, maximum: 20 },
  },
  required: ["query"],
  additionalProperties: false,
};

const DOCUMENT_PAGE_SIZE = 50;
const GraphCanvas3D = lazy(() => import("./GraphCanvas3D"));
const DOCUMENT_ACCEPT = ".md,.markdown,.txt,.html,.htm,.rst,.adoc,.log,.csv,.tsv,.json,.jsonl,.yaml,.yml,.xml,.properties";
const SERVICE_FACET_KEYS = ["repository", "index", "build", "state", "owner", "interfaces", "submodules"] as const;

type ServiceFacetKey = typeof SERVICE_FACET_KEYS[number];
type ServiceFacetOption = { value: string; label: string };
type ServiceFilterRecord = {
  key: string;
  searchText: string;
  facets: Record<ServiceFacetKey, ServiceFacetOption[]>;
};

const SERVICE_FACET_LABELS: Record<ServiceFacetKey, string> = {
  repository: "Репозиторий",
  index: "Индекс",
  build: "Build",
  state: "Состояние",
  owner: "Владелец",
  interfaces: "Интерфейсы",
  submodules: "Подмодули",
};

function emptyServiceFilters(): Record<ServiceFacetKey, string[]> {
  return {
    repository: [],
    index: [],
    build: [],
    state: [],
    owner: [],
    interfaces: [],
    submodules: [],
  };
}

function facet(value: string, label: string): ServiceFacetOption {
  return { value, label };
}

function number(value: number): string {
  return new Intl.NumberFormat("ru-RU").format(value ?? 0);
}

function localToolCount(overview: Overview): number {
  return overview.mcp_servers.servers.find((server) => server.id === "local")?.tool_count
    ?? overview.managed_tools.tool_count;
}

function relativeDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return "только что";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч назад`;
  return date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

function short(value: string, limit = 48): string {
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function fileSize(value: number): string {
  if (value < 1024) return `${value} Б`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} КБ`;
  return `${(value / (1024 * 1024)).toFixed(1)} МБ`;
}

function repositoryNameFromGitUrl(value: string): string {
  const clean = value.trim().replace(/[?#].*$/, "").replace(/\/+$/, "");
  if (!clean) return "";
  const segment = clean.split(/[/:]/).filter(Boolean).at(-1) || "";
  const name = segment.replace(/\.git$/i, "");
  try {
    return decodeURIComponent(name);
  } catch {
    return name;
  }
}

function repositorySourceKey(value: string): string {
  const clean = value.trim().replace(/\\/g, "/").replace(/\/+$/, "");
  const stripGitSuffix = (path: string) => {
    let decoded = path;
    try {
      decoded = decodeURIComponent(path);
    } catch {
      // Keep malformed percent escapes intact so typing cannot break the modal.
    }
    return decoded
      .replace(/^\/+|\/+$/g, "")
      .replace(/\.git$/i, "")
      .toLocaleLowerCase();
  };
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(clean)) {
    try {
      const parsed = new URL(clean);
      if (parsed.protocol === "file:") return `local:${stripGitSuffix(parsed.pathname)}`;
      return `remote:${parsed.hostname.toLocaleLowerCase()}/${stripGitSuffix(parsed.pathname)}`;
    } catch {
      return clean.toLocaleLowerCase();
    }
  }
  const scpMatch = clean.match(/^(?:[^@/:]+@)?([^:]+):(.+)$/);
  if (scpMatch) {
    return `remote:${scpMatch[1].toLocaleLowerCase()}/${stripGitSuffix(scpMatch[2])}`;
  }
  return `local:${clean}`;
}

type RepositoryBatchRow = {
  id: string;
  name: string;
  gitUrl: string;
  branch: string | null;
  indexId: string | null;
  skipReason: string | null;
};

type RepositoryBatchJobResponse = CatalogJob & {
  generation_mode: "static" | "gigacode";
  fallback_reason: string | null;
  repository_count: number;
  scheduled_count: number;
  skipped_count: number;
  worker_count: number;
};

type JobHistoryResponse = {
  total: number;
  active_count: number;
  failed_count: number;
  log_file_count: number;
  log_bytes: number;
  jobs: CatalogJob[];
};

function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  const source = text.replace(/^\uFEFF/, "");

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (character === '"') {
      if (quoted && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      row.push(field);
      field = "";
    } else if ((character === "\n" || character === "\r") && !quoted) {
      if (character === "\r" && source[index + 1] === "\n") index += 1;
      row.push(field);
      if (row.some((value) => value.trim())) rows.push(row);
      row = [];
      field = "";
    } else {
      field += character;
    }
  }
  if (quoted) throw new Error("В CSV не закрыта кавычка");
  row.push(field);
  if (row.some((value) => value.trim())) rows.push(row);
  return rows;
}

function resolveRepositoryCsvIndex(
  value: string,
  indexes: RagIndex[],
  rowNumber: number,
): string | null {
  const requested = value.trim();
  if (!requested) return null;
  const normalized = requested.toLocaleLowerCase();
  const idMatch = indexes.find((index) => index.id.toLocaleLowerCase() === normalized);
  if (idMatch) return idMatch.id;
  const nameMatches = indexes.filter((index) => index.name.toLocaleLowerCase() === normalized);
  if (nameMatches.length === 1) return nameMatches[0].id;
  if (nameMatches.length > 1) {
    throw new Error(`Строка ${rowNumber}: название индекса «${requested}» неоднозначно, укажите ID`);
  }
  throw new Error(`Строка ${rowNumber}: индекс «${requested}» не найден`);
}

function parseRepositoryCsv(
  text: string,
  indexes: RagIndex[],
  repositories: RepositorySource[],
): RepositoryBatchRow[] {
  const rows = parseCsv(text);
  if (!rows.length) throw new Error("CSV пустой");
  const header = rows[0].map((value) => value.trim().toLocaleLowerCase());
  const allowedColumns = new Set(["git", "branch", "index"]);
  const unknownColumns = header.filter((column) => !allowedColumns.has(column));
  if (unknownColumns.length) {
    throw new Error(`Неизвестные колонки CSV: ${Array.from(new Set(unknownColumns)).join(", ")}`);
  }
  if (new Set(header).size !== header.length) throw new Error("В CSV повторяются названия колонок");
  const gitColumn = header.indexOf("git");
  if (gitColumn < 0) throw new Error("В CSV обязательна колонка git");
  const branchColumn = header.indexOf("branch");
  const indexColumn = header.indexOf("index");
  if (rows.length === 1) throw new Error("В CSV нет Git-репозиториев");
  if (rows.length > 1001) throw new Error("За один запуск можно обработать не более 1000 репозиториев");

  const seen = new Set<string>();
  const existingBySource = new Map(
    repositories.map((repository) => [repositorySourceKey(repository.git_url), repository]),
  );
  return rows.slice(1).map((columns, index) => {
    const rowNumber = index + 2;
    if (columns.length > header.length) {
      throw new Error(`Строка ${rowNumber}: значений больше, чем колонок в заголовке`);
    }
    const gitUrl = (columns[gitColumn] || "").trim();
    if (!gitUrl) throw new Error(`Строка ${rowNumber}: Git URL пустой`);
    const sourceKey = repositorySourceKey(gitUrl);
    const existing = existingBySource.get(sourceKey);
    const skipReason = existing
      ? `Уже подключён: ${existing.name}`
      : seen.has(sourceKey)
        ? "Повторяется в CSV"
        : null;
    seen.add(sourceKey);
    const name = repositoryNameFromGitUrl(gitUrl);
    if (name.length < 2) throw new Error(`Строка ${rowNumber}: не удалось определить имя репозитория`);
    const branch = branchColumn >= 0 ? (columns[branchColumn] || "").trim() : "";
    const indexId = indexColumn >= 0
      ? resolveRepositoryCsvIndex(columns[indexColumn] || "", indexes, rowNumber)
      : null;
    return {
      id: `${index}-${gitUrl}`,
      name,
      gitUrl,
      branch: branch || null,
      indexId,
      skipReason,
    };
  });
}

function jobAuthenticationUrl(job: CatalogJob | null | undefined): string | null {
  const value = job?.result?.authentication_url;
  return typeof value === "string" && value.startsWith("http") ? value : null;
}

function Status({ value }: { value: string }) {
  const labels: Record<string, string> = {
    ready: "готов",
    completed: "готов",
    queued: "в очереди",
    running: "выполняется",
    cancelled: "отменено",
    cancelling: "отмена",
    failed: "ошибка",
    error: "ошибка",
    indexing: "индексация",
    empty: "ожидает",
    online: "в сети",
    offline: "не в сети",
    unchecked: "не проверен",
    "module-empty": "пустой модуль",
    unsupported: "не поддерживается",
    pending: "ожидает",
    skipped: "пропуск",
    starting: "подключение",
  };
  return (
    <span className={`status status-${value}`}>
      <span /> {labels[value] || value}
    </span>
  );
}

function Modal({ title, children, onClose, className = "", closeDisabled = false }: { title: string; children: ReactNode; onClose: () => void; className?: string; closeDisabled?: boolean }) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => { if (!closeDisabled) onClose(); }}>
      <section className={`modal ${className}`.trim()} role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <div>
            <span className="eyebrow">RAG control plane</span>
            <h2>{title}</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Закрыть" disabled={closeDisabled}>×</button>
        </header>
        {children}
      </section>
    </div>
  );
}

function Login({ error, onSubmit }: { error: string; onSubmit: (password: string) => void }) {
  const [password, setPassword] = useState("");
  return (
    <main className="login-shell">
      <section className="login-card">
        <div className="brand-mark large">R</div>
        <span className="eyebrow">Corporate knowledge infrastructure</span>
        <h1>RAG Control Plane</h1>
        <p>Управление индексами, Git/OpenSpec-источниками и MCP-инструментами.</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit(password);
          }}
        >
          <label>
            Пароль администратора
            <input
              autoFocus
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="KB_ADMIN_PASSWORD"
            />
          </label>
          {error && <div className="form-error">{error}</div>}
          <button className="button primary wide" type="submit">Открыть панель <span>→</span></button>
        </form>
        <small>Пароль хранится только в текущей вкладке браузера.</small>
      </section>
    </main>
  );
}

function Startup({ error, loading, onRetry }: { error: string; loading: boolean; onRetry: () => void }) {
  return (
    <main className="login-shell">
      <section className="login-card">
        <div className="brand-mark large">R</div>
        <span className="eyebrow">Corporate knowledge infrastructure</span>
        <h1>RAG Control Plane</h1>
        <p>{loading ? "Подключаемся к локальному RAG-серверу…" : "Сервер пока недоступен."}</p>
        {error && <div className="form-error">{error}</div>}
        {!loading && <button className="button primary wide" onClick={onRetry}>Повторить <span>→</span></button>}
      </section>
    </main>
  );
}

export default function App() {
  const [password, setPassword] = useState(() => sessionStorage.getItem("rag-admin-password") || "");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [page, setPage] = useState<Page>("overview");
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [loading, setLoading] = useState(false);
  const [indexModal, setIndexModal] = useState(false);
  const [selectedIndexId, setSelectedIndexId] = useState<string | null>(null);
  const [repositoryModal, setRepositoryModal] = useState(false);
  const [toolModal, setToolModal] = useState<ManagedTool | "new" | null>(null);
  const [builtinToolModal, setBuiltinToolModal] = useState<ToolCatalogItem | null>(null);
  const [serverModal, setServerModal] = useState(false);
  const [ssotModal, setSsotModal] = useState<{
    service: Overview["service_map"]["services"][number];
    defaultIndexId: string;
  } | null>(null);
  const [systemSsotModal, setSystemSsotModal] = useState(false);
  const [booting, setBooting] = useState(true);
  const [accessDenied, setAccessDenied] = useState(false);

  const load = useCallback(async () => {
    try {
      const payload = await api<Overview>("/admin/api/overview", password);
      setOverview(payload);
      setError("");
      setAccessDenied(false);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) {
        sessionStorage.removeItem("rag-admin-password");
        setOverview(null);
        setAccessDenied(true);
        setError(caught.message);
      } else {
        setAccessDenied(false);
        setError(caught instanceof Error ? caught.message : "Не удалось загрузить панель");
      }
    } finally {
      setBooting(false);
    }
  }, [password]);

  useEffect(() => void load(), [load]);
  const hasActiveJobs = Boolean(
    overview?.catalog.jobs.some((job) => ["queued", "running", "cancelling"].includes(job.status)),
  );
  useEffect(() => {
    let disposed = false;
    let timer = 0;
    const poll = async () => {
      await load();
      if (!disposed) timer = window.setTimeout(poll, hasActiveJobs ? 1000 : 5000);
    };
    timer = window.setTimeout(poll, hasActiveJobs ? 1000 : 5000);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [hasActiveJobs, load]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 3600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const submitPassword = (value: string) => {
    sessionStorage.setItem("rag-admin-password", value);
    setAccessDenied(false);
    setBooting(true);
    if (value === password) void load();
    else setPassword(value);
  };

  const action = async (run: () => Promise<unknown>, message: string) => {
    setLoading(true);
    try {
      await run();
      setToast(message);
      await load();
    } catch (caught) {
      setToast(caught instanceof Error ? caught.message : "Операция завершилась ошибкой");
    } finally {
      setLoading(false);
    }
  };

  if (!overview) {
    if (accessDenied) return <Login error={error} onSubmit={submitPassword} />;
    return (
      <Startup
        error={error}
        loading={booting}
        onRetry={() => {
          setBooting(true);
          void load();
        }}
      />
    );
  }

  const title = NAV.find((item) => item.id === page)?.label ?? "RAG Control Plane";
  const authenticationJob = overview.catalog.jobs.find(
    (job) => job.status === "running"
      && job.result?.phase === "awaiting_authentication"
      && jobAuthenticationUrl(job),
  );
  const authenticationUrl = jobAuthenticationUrl(authenticationJob);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">R</span><div><b>RAG</b><small>CONTROL PLANE</small></div></div>
        <nav>
          <span className="nav-label">Рабочая область</span>
          {NAV.map((item) => (
            <button key={item.id} className={page === item.id ? "active" : ""} onClick={() => { setPage(item.id); if (item.id !== "indexes") setSelectedIndexId(null); }}>
              <span className="nav-mark">{item.mark}</span>{item.label}
              {item.id === "services" && <em>{Math.max(overview.service_map.service_count, overview.catalog.repository_count)}</em>}
              {item.id === "servers" && <em>{overview.mcp_servers.server_count}</em>}
              {item.id === "tools" && <em>{localToolCount(overview)}</em>}
              {item.id === "operations" && <em>{overview.catalog.jobs.filter((job) => ["queued", "running", "cancelling", "failed"].includes(job.status)).length}</em>}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="server-state"><span className="pulse" /><div><b>MCP online</b><small>{overview.index.embedding_provider} embeddings</small></div></div>
          {password && (
            <button
              className="logout"
              onClick={() => {
                sessionStorage.removeItem("rag-admin-password");
                setPassword("");
                setOverview(null);
                setBooting(true);
              }}
            >Сменить доступ</button>
          )}
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div><span className="breadcrumb">RAG Control Plane /</span><h1>{title}</h1></div>
          <div className="top-actions">
            <button className="button quiet" onClick={() => void load()} disabled={loading}>↻ Обновить</button>
          {page === "indexes" && selectedIndexId && <button className="button quiet" onClick={() => setSelectedIndexId(null)}>← Все индексы</button>}
          {page === "indexes" && !selectedIndexId && <button className="button primary" onClick={() => setRepositoryModal(true)}>＋ Подключить репозиторий</button>}
            {page === "services" && (
              <button
                className="button secondary"
                disabled={loading || overview.catalog.jobs.some((job) => job.type === "ssot" && job.target_id === "all-services" && ["queued", "running", "cancelling"].includes(job.status))}
                title="OpenSpec загружается напрямую; остальные сервисы анализируются через GigaCode, если он доступен, иначе статически. Затем обновляются привязанные индексы."
                onClick={() => void action(
                  () => post("/admin/api/services/refresh-all", password, {}),
                  "Обновление SSOT всех сервисов поставлено в очередь",
                )}
              >↻ Обновить все SSOT</button>
            )}
            {page === "services" && <button className="button primary" onClick={() => setSystemSsotModal(true)}>✦ Подготовить SSOT-контекст</button>}
            {page === "servers" && <button className="button primary" onClick={() => setServerModal(true)}>＋ Добавить MCP server</button>}
            {page === "tools" && <button className="button primary" onClick={() => setToolModal("new")}>＋ Новый MCP tool</button>}
          </div>
        </header>

        {authenticationJob && authenticationUrl && (
          <section className="authentication-banner" role="alert" aria-live="assertive">
            <span className="authentication-mark">✦</span>
            <div className="grow">
              <b>GigaCode ожидает авторизацию</b>
              <small>Откройте ссылку, завершите вход в браузере — текущая задача продолжится автоматически, перезапускать анализ не нужно.</small>
            </div>
            <button className="button quiet" onClick={() => setPage("operations")}>Открыть лог</button>
            <a className="button primary" href={authenticationUrl} target="_blank" rel="noreferrer">Войти в GigaCode ↗</a>
          </section>
        )}

        <section className="content">
          {page === "overview" && <OverviewPage data={overview} password={password} onNavigate={setPage} onAction={action} />}
          {page === "indexes" && selectedIndexId && overview.catalog.indexes.some((index) => index.id === selectedIndexId) ? (
            <IndexDetailPage
              index={overview.catalog.indexes.find((index) => index.id === selectedIndexId)!}
              repositories={overview.catalog.repositories.filter((repository) => repository.index_id === selectedIndexId)}
              password={password}
              onBack={() => setSelectedIndexId(null)}
              onChanged={(message) => {
                setToast(message);
                void load();
              }}
              onAction={action}
            />
          ) : page === "indexes" && (
            <IndexesPage
              data={overview}
              password={password}
              onCreate={() => setIndexModal(true)}
              onRepository={() => setRepositoryModal(true)}
              onOpen={setSelectedIndexId}
              onAction={action}
            />
          )}
          {page === "services" && (
            <ServicesPage
              data={overview}
              password={password}
              onGraph={() => setPage("graph")}
              onSsot={(service, defaultIndexId) => setSsotModal({ service, defaultIndexId })}
              onAction={action}
            />
          )}
          {page === "servers" && (
            <ServersPage data={overview} password={password} onAction={action} />
          )}
          {page === "tools" && (
            <ToolsPage
              data={overview}
              password={password}
              onEditManaged={setToolModal}
              onEditBuiltin={setBuiltinToolModal}
              onAction={action}
            />
          )}
          {page === "graph" && (
            <GraphPage
              data={overview.graph}
              password={password}
              gigacodeAvailable={overview.catalog.ssot_generation.gigacode.available}
              gigacodeError={overview.catalog.ssot_generation.gigacode.error}
              onAction={action}
            />
          )}
          {page === "operations" && <OperationsPage data={overview} password={password} onAction={action} />}
        </section>
      </main>

      {indexModal && (
        <IndexForm
          password={password}
          onClose={() => setIndexModal(false)}
          onSaved={() => {
            setIndexModal(false);
            setToast("Индекс создан");
            void load();
          }}
        />
      )}
      {repositoryModal && (
        <RepositoryForm
          password={password}
          indexes={overview.catalog.indexes}
          repositories={overview.catalog.repositories}
          gigacodeAvailable={overview.catalog.ssot_generation.gigacode.available}
          gigacodeError={overview.catalog.ssot_generation.gigacode.error}
          onClose={() => setRepositoryModal(false)}
          onSaved={(mode) => {
            setRepositoryModal(false);
            setToast(mode === "gigacode" ? "Импорт и GigaCode-анализ поставлены в очередь" : "Импорт поставлен в очередь");
            void load();
          }}
          onBatchStarted={(count, scheduled, skipped, workers, mode, fallbackReason) => {
            setRepositoryModal(false);
            setToast(
              scheduled === 0
                ? `Все ${count} репозиториев уже подключены — сканирование не запускалось`
                : fallbackReason
                  ? `Запущено ${scheduled} из ${count} на ${workers} воркерах, пропущено ${skipped}; static-режим: ${fallbackReason}`
                  : `Запущено ${scheduled} из ${count} на ${workers} воркерах, пропущено ${skipped}${mode === "gigacode" ? ", с GigaCode" : ""}`,
            );
            void load();
          }}
        />
      )}
      {toolModal && (
        <ToolForm
          password={password}
          indexes={overview.catalog.indexes}
          tool={toolModal === "new" ? null : toolModal}
          onClose={() => setToolModal(null)}
          onSaved={() => {
            setToolModal(null);
            setToast("MCP tool сохранён");
            void load();
          }}
        />
      )}
      {builtinToolModal && (
        <BuiltinToolForm
          password={password}
          tool={builtinToolModal}
          onClose={() => setBuiltinToolModal(null)}
          onSaved={() => { setBuiltinToolModal(null); void load(); }}
        />
      )}
      {serverModal && (
        <ServerForm
          password={password}
          onClose={() => setServerModal(false)}
          onSaved={() => {
            setServerModal(false);
            setToast("MCP server добавлен и проверен");
            void load();
          }}
        />
      )}
      {ssotModal && (
        <SsotModal
          password={password}
          service={ssotModal.service}
          indexes={overview.catalog.indexes}
          defaultIndexId={ssotModal.defaultIndexId}
          onClose={() => setSsotModal(null)}
          onImported={() => {
            setSsotModal(null);
            setToast("SSOT сохранён, переиндексация запущена");
            void load();
          }}
        />
      )}
      {systemSsotModal && (
        <SystemSsotModal
          password={password}
          indexes={overview.catalog.indexes}
          generator={overview.catalog.ssot_generation}
          serviceCount={overview.service_map.service_count}
          onClose={() => setSystemSsotModal(false)}
          onStarted={() => {
            setSystemSsotModal(false);
            setToast("Подготовка исходников для клиентской модели поставлена в очередь");
            void load();
          }}
        />
      )}
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function OverviewPage({ data, password, onNavigate, onAction }: { data: Overview; password: string; onNavigate: (page: Page) => void; onAction: (run: () => Promise<unknown>, message: string) => Promise<void> }) {
  const activeJobs = data.catalog.jobs.filter((job) => ["queued", "running", "cancelling"].includes(job.status));
  return (
    <>
      <section className="hero-panel">
        <div>
          <span className="eyebrow">Knowledge infrastructure</span>
          <h2>Контекст системы под контролем</h2>
          <p>Индексы собирают OpenSpec из репозиториев, а MCP tools отдают агентам только нужный слой знаний.</p>
        </div>
        <div className="hero-orbit"><span /><span /><span /><b>{data.catalog.index_count}</b><small>индекса</small></div>
      </section>
      <div className="metric-row">
        <Metric label="Документы" value={number(data.index.document_count)} note={`${number(data.index.chunk_count)} чанков`} tone="lime" />
        <Metric label="MCP tools" value={number(localToolCount(data))} note={`${data.managed_tools.tool_count} управляемых`} tone="violet" />
        <Metric label="Git-источники" value={number(data.catalog.repository_count)} note={`${data.catalog.index_count} индексов`} tone="blue" />
        <Metric label="Вызовы" value={number(data.usage.total_calls)} note={`${data.usage.calls_last_minute} за минуту`} tone="amber" />
      </div>
      <div className="dashboard-grid">
        <section className="panel span-2">
          <PanelHeader title="Индексы знаний" kicker="Текущее состояние" action="Все индексы" onAction={() => onNavigate("indexes")} />
          <div className="compact-indexes">
            {data.catalog.indexes.slice(0, 5).map((index) => <IndexRow key={index.id} index={index} />)}
          </div>
        </section>
        <section className="panel">
          <PanelHeader title="Система" kicker="Процесс" />
          <div className="system-meter"><div style={{ "--meter": `${Math.max(3, data.server_metrics.load_percent)}%` } as React.CSSProperties} /></div>
          <dl className="system-list">
            <div><dt>Нагрузка</dt><dd>{data.server_metrics.load_percent}%</dd></div>
            <div><dt>Peak RAM</dt><dd>{data.server_metrics.peak_rss_mb} MB</dd></div>
            <div><dt>Uptime</dt><dd>{Math.floor(data.server_metrics.uptime_seconds / 60)} мин</dd></div>
            <div><dt>Граф</dt><dd>{number(data.graph.node_count)} узлов</dd></div>
          </dl>
        </section>
        <section className="panel span-3">
          <PanelHeader title="Последние операции" kicker={activeJobs.length ? `${activeJobs.length} выполняется` : "Очередь свободна"} />
          <JobList password={password} jobs={data.catalog.jobs.slice(0, 6)} onCancel={(job) => void onAction(() => post("/admin/api/jobs/cancel", password, { job_id: job.id }), "Отмена запрошена")} />
        </section>
      </div>
    </>
  );
}

function Metric({ label, value, note, tone }: { label: string; value: string; note: string; tone: string }) {
  return <article className={`metric-card ${tone}`}><span>{label}</span><strong>{value}</strong><small>{note}</small><i /></article>;
}

function PanelHeader({ title, kicker, action, onAction }: { title: string; kicker: string; action?: string; onAction?: () => void }) {
  return <header className="panel-header"><div><span>{kicker}</span><h3>{title}</h3></div>{action && <button onClick={onAction}>{action} →</button>}</header>;
}

function IndexRow({ index }: { index: RagIndex }) {
  return (
    <div className="index-row">
      <div className="index-glyph">{index.kind === "default" ? "◆" : "◇"}</div>
      <div className="grow"><b>{index.name}</b><small>{index.description || index.id}</small></div>
      <div className="index-stat"><b>{number(index.document_count)}</b><small>документов</small></div>
      <div className="index-stat"><b>{number(index.source_count)}</b><small>источников</small></div>
      <Status value={index.status} />
    </div>
  );
}

function JobList({ jobs, password, onCancel }: { jobs: CatalogJob[]; password: string; onCancel?: (job: CatalogJob) => void }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [logs, setLogs] = useState<Record<string, string>>({});
  const [logError, setLogError] = useState("");
  const loadLog = useCallback(async (jobId: string) => {
    try {
      const payload = await api<{ log: string }>(`/admin/api/jobs/log?job_id=${encodeURIComponent(jobId)}`, password);
      setLogs((current) => ({ ...current, [jobId]: payload.log || "Журнал пока пуст" }));
      setLogError("");
    } catch (caught) {
      setLogError(caught instanceof Error ? caught.message : "Не удалось загрузить журнал");
    }
  }, [password]);
  const expandedStatus = jobs.find((job) => job.id === expanded)?.status;
  useEffect(() => {
    if (!expanded) return;
    void loadLog(expanded);
    if (!expandedStatus || !["queued", "running", "cancelling"].includes(expandedStatus)) return;
    const timer = window.setInterval(() => void loadLog(expanded), 1000);
    return () => window.clearInterval(timer);
  }, [expanded, expandedStatus, loadLog]);
  const toggleLog = (job: CatalogJob) => {
    if (expanded === job.id) {
      setExpanded(null);
      return;
    }
    setExpanded(job.id);
    setLogError("");
  };
  if (!jobs.length) return <div className="empty-state compact">Операций пока не было</div>;
  return <div className="job-list">{jobs.map((job) => {
    const authenticationUrl = jobAuthenticationUrl(job);
    return <div className="job-item" key={job.id}>
      <div className="job-row">
        <span className={`job-icon ${job.type}`}>{job.type === "repository" ? "↗" : job.type === "graph" || job.type === "service" ? "⌘" : job.type === "ssot" ? "✦" : job.type === "cleanup" ? "×" : "◇"}</span>
        <div className="grow"><b>{job.message}</b><small>{job.error || `${job.type} · ${relativeDate(job.completed_at || job.started_at)}`}</small></div>
        <Status value={job.status} />
        {job.log_path
          ? <button className="job-log-button" onClick={() => toggleLog(job)}>{expanded === job.id ? "Скрыть лог" : "Полный лог"}</button>
          : <span className="job-log-missing" title="Эта задача была создана до появления постоянных job-логов">Лог не сохранён</span>}
        {authenticationUrl && job.status === "running" && <a className="job-log-button" href={authenticationUrl} target="_blank" rel="noreferrer">Войти в GigaCode ↗</a>}
        {onCancel && ["queued", "running", "cancelling"].includes(job.status) && <button className="job-cancel" disabled={job.status === "cancelling"} onClick={() => onCancel(job)}>{job.status === "cancelling" ? "Отменяем…" : "Отменить"}</button>}
      </div>
      {expanded === job.id && <div className="job-log"><div className="job-log-meta"><code>{job.id}</code><span>{job.log_path}</span></div><pre>{logError || logs[job.id] || "Загружаем журнал…"}</pre></div>}
    </div>;
  })}</div>;
}

function OperationsPage({ data, password, onAction }: {
  data: Overview;
  password: string;
  onAction: (run: () => Promise<unknown>, message: string) => Promise<void>;
}) {
  const [history, setHistory] = useState<JobHistoryResponse | null>(null);
  const [historyError, setHistoryError] = useState("");
  const overviewActive = data.catalog.jobs.filter((job) => ["queued", "running", "cancelling"].includes(job.status)).length;
  const loadHistory = useCallback(async () => {
    try {
      setHistory(await api<JobHistoryResponse>("/admin/api/jobs", password));
      setHistoryError("");
    } catch (caught) {
      setHistoryError(caught instanceof Error ? caught.message : "Не удалось загрузить историю операций");
    }
  }, [password]);
  useEffect(() => {
    void loadHistory();
    const timer = window.setInterval(() => void loadHistory(), overviewActive ? 1000 : 5000);
    return () => window.clearInterval(timer);
  }, [loadHistory, overviewActive]);
  const jobs = history?.jobs ?? data.catalog.jobs;
  const active = history?.active_count ?? overviewActive;
  const failed = history?.failed_count ?? jobs.filter((job) => job.status === "failed").length;
  const clearHistory = async () => {
    if (!window.confirm(
      "Очистить всю завершённую историю операций и физически удалить файлы из .cache/kb/job-logs? Активные задачи останутся в списке, но их накопленные логи тоже будут очищены.",
    )) return;
    await onAction(
      () => post("/admin/api/jobs/clear", password, {}),
      "История операций и файлы job-логов очищены",
    );
    await loadHistory();
  };
  return (
    <>
      <div className="section-intro">
        <div>
          <span className="eyebrow">Live diagnostics</span>
          <h2>Операции и полные логи</h2>
          <p>Откройте job: активный лог обновляется каждую секунду и показывает репозиторий, Java/Kotlin-файлы, cache hit/miss, длительность этапов и полный traceback.</p>
        </div>
        <div className="server-summary">
          <span><b>{active}</b> выполняется</span>
          <span><b>{failed}</b> с ошибкой</span>
          <span><b>{history?.total ?? jobs.length}</b> всего</span>
        </div>
      </div>
      <div className="log-location-grid">
        <article className="panel log-location"><span className="eyebrow">Job logs</span><code>.cache/kb/job-logs/&lt;job-id&gt;.log</code><small>Поток анализа и полный Python traceback для каждой новой операции.</small></article>
        <article className="panel log-location"><span className="eyebrow">Backend process</span><code>.cache/kb/runtime/mcp-http.log</code><small>Старт, HTTP-сервер и ошибки главного процесса при запуске через общий скрипт.</small></article>
        <article className="panel log-location"><span className="eyebrow">Successful runs</span><code>.cache/kb/analysis/runs/</code><small>Полные JSON-снимки завершённых анализов для дальнейшего SSOT.</small></article>
      </div>
      <section className="panel operations-panel">
        <header className="panel-header operations-header">
          <div><span>{active ? `Сейчас выполняется: ${active}` : "Активных задач нет"}</span><h3>Все фоновые задачи</h3></div>
          <div className="operations-actions">
            <small>{history?.log_file_count ?? 0} файлов · {fileSize(history?.log_bytes ?? 0)}</small>
            <button className="operations-clear" disabled={!jobs.length && !history?.log_file_count} onClick={() => void clearHistory()}>Очистить историю и логи</button>
          </div>
        </header>
        {historyError && <div className="inline-error operations-error">{historyError}</div>}
        <div className="operations-scroll">
          <JobList password={password} jobs={jobs} onCancel={(job) => void onAction(() => post("/admin/api/jobs/cancel", password, { job_id: job.id }), "Отмена запрошена")} />
        </div>
      </section>
    </>
  );
}

function IndexesPage({ data, password, onCreate, onRepository, onOpen, onAction }: {
  data: Overview;
  password: string;
  onCreate: () => void;
  onRepository: () => void;
  onOpen: (indexId: string) => void;
  onAction: (run: () => Promise<unknown>, message: string) => Promise<void>;
}) {
  const nameById = useMemo(() => Object.fromEntries(data.catalog.indexes.map((item) => [item.id, item.name])), [data]);
  return (
    <>
      <div className="section-intro">
        <div><span className="eyebrow">Retrieval topology</span><h2>Индексы и источники</h2><p>Изолируйте домены знаний или объединяйте несколько индексов одним MCP-инструментом.</p></div>
        <button className="button secondary" onClick={onCreate}>◇ Создать пустой индекс</button>
      </div>
      <div className="index-card-grid">
        {data.catalog.indexes.map((index) => (
          <article
            className="index-card clickable"
            key={index.id}
            role="button"
            tabIndex={0}
            onClick={() => onOpen(index.id)}
            onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onOpen(index.id); }}
          >
            <header><div className="index-glyph big">{index.kind === "default" ? "◆" : "◇"}</div><Status value={index.status} /></header>
            <h3>{index.name}</h3><p>{index.description || "Отдельный контур корпоративных знаний"}</p>
            <div className="index-numbers"><span><b>{number(index.document_count)}</b> документов</span><span><b>{number(index.chunk_count)}</b> чанков</span><span><b>{index.source_count}</b> Git</span></div>
            {index.error && <div className="inline-error">{index.error}</div>}
            <footer><small>Обновлён {relativeDate(index.updated_at)} · открыть →</small><button onClick={(event) => { event.stopPropagation(); void onAction(() => post("/admin/api/indexes/build", password, { index_id: index.id }), "Переиндексация запущена"); }}>↻ Пересобрать</button></footer>
          </article>
        ))}
      </div>
      <section className="panel repositories-panel">
        <PanelHeader title="Подключённые репозитории" kicker="Git → OpenSpec → RAG" action="Подключить" onAction={onRepository} />
        {data.catalog.repositories.length ? (
          <div className="table-wrap"><table><thead><tr><th>Репозиторий</th><th>Ветка / commit</th><th>Индекс</th><th>OpenSpec</th><th>Хранение</th><th>Синхронизация</th><th /></tr></thead><tbody>
            {data.catalog.repositories.map((repo) => <tr key={repo.id}><td><b>{repo.name}</b><small title={repo.git_url}>{short(repo.git_url, 56)}</small></td><td><code>{repo.ref || "HEAD"}</code><small>{repo.commit?.slice(0, 9) || "—"}</small></td><td><span className="tag">{nameById[repo.index_id] || repo.index_id}</span></td><td><b>{repo.document_count}</b><small>документов</small></td><td><b>{repo.checkout_state === "removed" ? "Только документация" : repo.checkout_state === "external" ? "Внешний источник" : "Checkout доступен"}</b><small>{repo.checkout_state === "removed" ? "локальный clone удалён" : "исходники не копируются в RAG"}</small></td><td>{relativeDate(repo.synced_at)}</td><td><button className="table-danger" onClick={() => { if (!window.confirm(`Удалить repository «${repo.name}», его документы из RAG и сервисы из карты?`)) return; void onAction(() => post("/admin/api/repositories/delete", password, { repository_id: repo.id }), "Удаление repository поставлено в очередь"); }}>Удалить</button></td></tr>)}
          </tbody></table></div>
        ) : <div className="empty-state"><div>↗</div><h3>Подключите первый репозиторий</h3><p>Сервис проанализирует исходники, найдёт openspec при наличии, обновит индекс и карту.</p><button className="button primary" onClick={onRepository}>Подключить Git</button></div>}
      </section>
      {data.catalog.jobs.length > 0 && <section className="panel"><PanelHeader title="Очередь операций" kicker="Фоновые задачи" /><JobList password={password} jobs={data.catalog.jobs} onCancel={(job) => void onAction(() => post("/admin/api/jobs/cancel", password, { job_id: job.id }), "Отмена запрошена")} /></section>}
    </>
  );
}

function IndexDetailPage({ index, repositories, password, onBack, onChanged, onAction }: {
  index: RagIndex;
  repositories: Overview["catalog"]["repositories"];
  password: string;
  onBack: () => void;
  onChanged: (message: string) => void;
  onAction: (run: () => Promise<unknown>, message: string) => Promise<void>;
}) {
  const [documents, setDocuments] = useState<IndexDocumentsPage | null>(null);
  const [selectedDocument, setSelectedDocument] = useState<IndexDocument | null>(null);
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [files, setFiles] = useState<File[]>([]);
  const [overwrite, setOverwrite] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [loadingDocuments, setLoadingDocuments] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");

  const loadDocuments = useCallback(async () => {
    setLoadingDocuments(true);
    try {
      const params = new URLSearchParams({
        index_id: index.id,
        offset: String(offset),
        limit: String(DOCUMENT_PAGE_SIZE),
      });
      if (query) params.set("query", query);
      const payload = await api<IndexDocumentsPage>(`/admin/api/indexes/documents?${params}`, password);
      setDocuments(payload);
      setError("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось загрузить документы индекса");
    } finally {
      setLoadingDocuments(false);
    }
  }, [index.id, index.updated_at, offset, password, query]);

  useEffect(() => void loadDocuments(), [loadDocuments]);

  const selectFiles = (incoming: FileList | File[]) => {
    const selected = Array.from(incoming);
    const allowed = new Set(DOCUMENT_ACCEPT.split(","));
    const unsupported = selected.find((file) => {
      const dot = file.name.lastIndexOf(".");
      return dot < 0 || !allowed.has(file.name.slice(dot).toLowerCase());
    });
    if (unsupported) {
      setError(`Неподдерживаемый текстовый формат: ${unsupported.name}`);
      return;
    }
    if (selected.length > 50) {
      setError("За один раз можно загрузить не больше 50 файлов");
      return;
    }
    const unique = new Map(selected.map((file) => [file.name, file]));
    setFiles([...unique.values()]);
    setError("");
  };

  const upload = async () => {
    if (!files.length) return;
    setUploading(true);
    setError("");
    try {
      const payload = await Promise.all(files.map(async (file) => ({
        path: file.name,
        content: await file.text(),
      })));
      await post(
        "/admin/api/indexes/documents",
        password,
        { index_id: index.id, documents: payload, overwrite },
        30_000,
      );
      const count = files.length;
      setFiles([]);
      setOverwrite(false);
      onChanged(`${count} файл(а) загружено; пересборка индекса поставлена в очередь`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось загрузить документы");
    } finally {
      setUploading(false);
    }
  };

  const origins: Record<string, string> = {
    repository: "Git / OpenSpec",
    upload: "Загружен вручную",
    ssot: "SSOT",
    local: "Локальный источник",
  };
  const totalSelectedBytes = files.reduce((sum, file) => sum + file.size, 0);

  return (
    <>
      <div className="index-detail-head">
        <button className="back-link" onClick={onBack}>← Индексы</button>
        <div className="index-detail-title">
          <div className="index-glyph big">{index.kind === "default" ? "◆" : "◇"}</div>
          <div><span className="eyebrow">Index · {index.id}</span><h2>{index.name}</h2><p>{index.description || "Отдельный контур корпоративных знаний"}</p></div>
          <Status value={index.status} />
        </div>
        <div className="index-detail-metrics">
          <span><b>{number(index.document_count)}</b> документов</span>
          <span><b>{number(index.chunk_count)}</b> чанков</span>
          <span><b>{repositories.length}</b> Git-источников</span>
          <button className="button secondary" onClick={() => void onAction(() => post("/admin/api/indexes/build", password, { index_id: index.id }), "Переиндексация запущена")}>↻ Пересобрать индекс</button>
        </div>
      </div>

      <section className="panel index-upload-panel">
        <PanelHeader title="Догрузить текстовые файлы" kicker="Файлы сохранятся только в этом индексе" />
        <div className="upload-layout">
          <label
            className={`file-drop ${dragging ? "dragging" : ""}`}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => { event.preventDefault(); setDragging(false); selectFiles(event.dataTransfer.files); }}
          >
            <input type="file" multiple accept={DOCUMENT_ACCEPT} onChange={(event) => { if (event.target.files) selectFiles(event.target.files); event.currentTarget.value = ""; }} />
            <span className="drop-icon">＋</span>
            <b>Перетащите файлы или выберите на диске</b>
            <small>Markdown, TXT, HTML, JSON, YAML, CSV, XML и другие текстовые форматы · до 50 файлов</small>
          </label>
          <div className="upload-queue">
            <div className="upload-queue-head"><b>{files.length ? `${files.length} выбрано` : "Файлы не выбраны"}</b><small>{files.length ? `${(totalSelectedBytes / 1024).toFixed(1)} KB` : "После загрузки индекс пересоберётся автоматически"}</small></div>
            {files.length > 0 && <div className="selected-files">{files.slice(0, 6).map((file) => <span key={file.name}><b>{file.name}</b><small>{(file.size / 1024).toFixed(1)} KB</small></span>)}{files.length > 6 && <em>и ещё {files.length - 6}</em>}</div>}
            <label className="overwrite-check"><input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} /> Перезаписать файлы с одинаковым именем</label>
            <button className="button primary" disabled={!files.length || uploading} onClick={() => void upload()}>{uploading ? "Загружаем…" : "Загрузить и индексировать"}</button>
          </div>
        </div>
        {error && <div className="form-error index-detail-error">{error}</div>}
      </section>

      <section className="panel index-documents-panel">
        <div className="documents-toolbar">
          <div><span className="eyebrow">Serving index content</span><h3>Документы индекса</h3><p>{documents ? `${number(documents.total)} найдено` : "Загружаем список…"}</p></div>
          <form onSubmit={(event) => { event.preventDefault(); setOffset(0); setQuery(queryInput.trim()); }}><input value={queryInput} onChange={(event) => setQueryInput(event.target.value)} placeholder="Название или путь документа" /><button className="button secondary">Найти</button>{query && <button type="button" className="button quiet" onClick={() => { setQueryInput(""); setQuery(""); setOffset(0); }}>Сбросить</button>}</form>
        </div>
        {loadingDocuments && !documents ? <div className="empty-state"><div>◌</div><h3>Читаем индекс</h3></div> : documents?.documents.length ? (
          <div className="table-wrap"><table className="documents-table"><thead><tr><th>Документ</th><th>Источник</th><th>Тип</th><th>Загружен</th></tr></thead><tbody>{documents.documents.map((document) => <tr key={document.document_id}><td><button className="document-open" onClick={() => setSelectedDocument(document)}><b>{document.title}</b><small title={document.source_path}>{document.source_path}</small><em>Открыть →</em></button></td><td><span className={`tag origin-${document.origin}`}>{origins[document.origin] || document.origin}</span></td><td><code>{document.source_type}</code></td><td>{relativeDate(document.loaded_at)}</td></tr>)}</tbody></table></div>
        ) : <div className="empty-state"><div>◇</div><h3>{query ? "Ничего не найдено" : "Индекс пока пуст"}</h3><p>{query ? "Измените поисковый запрос." : "Загрузите текстовые файлы или подключите Git/OpenSpec."}</p></div>}
        {documents && documents.total > DOCUMENT_PAGE_SIZE && <div className="documents-pagination"><button className="button quiet" disabled={offset === 0 || loadingDocuments} onClick={() => setOffset(Math.max(0, offset - DOCUMENT_PAGE_SIZE))}>← Назад</button><span>{number(offset + 1)}–{number(Math.min(offset + documents.documents.length, documents.total))} из {number(documents.total)}</span><button className="button quiet" disabled={!documents.has_more || loadingDocuments} onClick={() => setOffset(offset + DOCUMENT_PAGE_SIZE)}>Дальше →</button></div>}
      </section>
      {selectedDocument && <DocumentViewer indexId={index.id} document={selectedDocument} password={password} onClose={() => setSelectedDocument(null)} />}
    </>
  );
}

function DocumentViewer({ indexId, document, password, onClose }: {
  indexId: string;
  document: IndexDocument;
  password: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<IndexDocumentDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let disposed = false;
    const params = new URLSearchParams({ index_id: indexId, document_id: document.document_id });
    api<IndexDocumentDetail>(`/admin/api/indexes/document?${params}`, password, {}, 30_000)
      .then((payload) => { if (!disposed) { setDetail(payload); setError(""); } })
      .catch((caught) => { if (!disposed) setError(caught instanceof Error ? caught.message : "Не удалось открыть документ"); });
    return () => { disposed = true; };
  }, [document.document_id, indexId, password]);

  const origins: Record<string, string> = {
    repository: "Git / OpenSpec",
    upload: "Загружен вручную",
    ssot: "SSOT",
    local: "Локальный источник",
  };

  return (
    <Modal title={document.title} onClose={onClose} className="document-modal">
      {error ? <div className="form-error document-view-error">{error}</div> : !detail ? (
        <div className="document-loading"><span>◌</span><b>Загружаем документ из индекса…</b></div>
      ) : (
        <div className="document-viewer">
          <div className="document-facts">
            <span><small>Индекс</small><b>{detail.index.name}</b></span>
            <span><small>Источник</small><b>{origins[detail.origin] || detail.origin}</b></span>
            <span><small>Тип</small><b>{detail.source_type}</b></span>
            <span><small>Размер</small><b>{number(detail.content_chars)} символов · {(detail.content_bytes / 1024).toFixed(1)} KB</b></span>
          </div>
          <div className="document-source-line"><code>{detail.source_path}</code>{detail.source_url && <a href={detail.source_url} target="_blank" rel="noreferrer">Открыть источник ↗</a>}</div>
          <section className="document-content"><div><span className="eyebrow">Normalized serving content</span><b>Содержимое документа</b></div><pre>{detail.content}</pre></section>
          <details className="document-metadata"><summary>Метаданные и идентификаторы</summary><pre>{JSON.stringify({ document_id: detail.document_id, source_id: detail.source_id, loaded_at: detail.loaded_at, metadata: detail.metadata }, null, 2)}</pre></details>
        </div>
      )}
    </Modal>
  );
}

function ServicesPage({ data, password, onGraph, onSsot, onAction }: {
  data: Overview;
  password: string;
  onGraph: () => void;
  onSsot: (service: Overview["service_map"]["services"][number], defaultIndexId: string) => void;
  onAction: (run: () => Promise<unknown>, message: string) => Promise<void>;
}) {
  const [serviceQuery, setServiceQuery] = useState("");
  const [serviceFilters, setServiceFilters] = useState<Record<ServiceFacetKey, string[]>>(
    emptyServiceFilters,
  );
  const indexNames = Object.fromEntries(
    data.catalog.indexes.map((index) => [index.id, index.name]),
  );
  const gigacode = data.catalog.ssot_generation.gigacode;
  const servicesByRepository = new Map<string, typeof data.service_map.services>();
  data.service_map.services.forEach((service) => {
    const key = service.repository_root || service.repository;
    const current = servicesByRepository.get(key) || [];
    servicesByRepository.set(key, [...current, service]);
  });
  const repositoryNames = new Set(data.catalog.repositories.map((repository) => repository.name));
  const repositoryRoots = new Set(data.catalog.repositories.map((repository) => repository.checkout_path));
  const standaloneMapServices = data.service_map.services.filter(
    (service) => !repositoryNames.has(service.repository)
      && (!service.repository_root || !repositoryRoots.has(service.repository_root)),
  );
  const serviceFilterRecords: ServiceFilterRecord[] = [];
  data.catalog.repositories.forEach((repository) => {
    const mappedServices = servicesByRepository.get(repository.checkout_path)
      || servicesByRepository.get(repository.name)
      || [];
    const repositoryFacet = facet(repository.id, repository.name);
    const indexFacet = facet(
      repository.index_id,
      indexNames[repository.index_id] || repository.index_id,
    );
    if (!mappedServices.length) {
      serviceFilterRecords.push({
        key: `repository:${repository.id}`,
        searchText: `${repository.name} ${repository.commit || ""}`.toLocaleLowerCase("ru"),
        facets: {
          repository: [repositoryFacet],
          index: [indexFacet],
          build: [facet("unknown", "Не определён")],
          state: [facet("awaiting", "Ожидает анализа")],
          owner: [facet("unassigned", "Не определён")],
          interfaces: [facet("none", "Нет интерфейсов")],
          submodules: [facet("none", "Нет подмодулей")],
        },
      });
      return;
    }
    mappedServices.forEach((service) => {
      const interfaceFacets: ServiceFacetOption[] = [];
      if (service.entrypoint_count) interfaceFacets.push(facet("entrypoints", "Есть входы"));
      if (service.outbound_interface_count) interfaceFacets.push(facet("outbound", "Есть выходы"));
      if (!interfaceFacets.length) interfaceFacets.push(facet("none", "Нет интерфейсов"));
      const stateLabels = { active: "Активный", empty: "Пустой модуль", unsupported: "Не поддерживается" };
      serviceFilterRecords.push({
        key: `${repository.id}:${service.id}`,
        searchText: `${service.name} ${service.id} ${service.module_path} ${repository.name} ${service.owner || ""}`.toLocaleLowerCase("ru"),
        facets: {
          repository: [repositoryFacet],
          index: [indexFacet],
          build: [service.module_state === "empty"
            ? facet("empty", "empty")
            : facet(service.build_system, service.build_system === "unknown" ? "Не определён" : service.build_system)],
          state: [facet(service.module_state, stateLabels[service.module_state])],
          owner: [facet(service.owner || "unassigned", service.owner || "Не определён")],
          interfaces: interfaceFacets,
          submodules: [service.component_paths.length ? facet("present", "Есть подмодули") : facet("none", "Нет подмодулей")],
        },
      });
    });
  });
  standaloneMapServices.forEach((service) => {
    const interfaceFacets: ServiceFacetOption[] = [];
    if (service.entrypoint_count) interfaceFacets.push(facet("entrypoints", "Есть входы"));
    if (service.outbound_interface_count) interfaceFacets.push(facet("outbound", "Есть выходы"));
    if (!interfaceFacets.length) interfaceFacets.push(facet("none", "Нет интерфейсов"));
    const repositoryLabel = service.repository || "Без репозитория";
    const repositoryValue = `standalone:${service.repository_root || repositoryLabel}`;
    const stateLabels = { active: "Активный", empty: "Пустой модуль", unsupported: "Не поддерживается" };
    serviceFilterRecords.push({
      key: `standalone:${service.id}`,
      searchText: `${service.name} ${service.id} ${service.module_path} ${repositoryLabel} ${service.owner || ""}`.toLocaleLowerCase("ru"),
      facets: {
        repository: [facet(repositoryValue, repositoryLabel)],
        index: [facet("unassigned", "Без индекса")],
        build: [service.module_state === "empty"
          ? facet("empty", "empty")
          : facet(service.build_system, service.build_system === "unknown" ? "Не определён" : service.build_system)],
        state: [facet(service.module_state, stateLabels[service.module_state])],
        owner: [facet(service.owner || "unassigned", service.owner || "Не определён")],
        interfaces: interfaceFacets,
        submodules: [service.component_paths.length ? facet("present", "Есть подмодули") : facet("none", "Нет подмодулей")],
      },
    });
  });
  const facetGroups = SERVICE_FACET_KEYS.map((key) => {
    const options = new Map<string, { label: string; count: number }>();
    serviceFilterRecords.forEach((record) => {
      record.facets[key].forEach((option) => {
        const current = options.get(option.value);
        options.set(option.value, { label: option.label, count: (current?.count || 0) + 1 });
      });
    });
    return {
      key,
      label: SERVICE_FACET_LABELS[key],
      options: [...options.entries()]
        .map(([value, option]) => ({ value, ...option }))
        .sort((left, right) => left.label.localeCompare(right.label, "ru")),
    };
  });
  const normalizedServiceQuery = serviceQuery.trim().toLocaleLowerCase("ru");
  const filteredServiceRecords = serviceFilterRecords.filter((record) => {
    if (normalizedServiceQuery && !record.searchText.includes(normalizedServiceQuery)) return false;
    return SERVICE_FACET_KEYS.every((key) => {
      const selected = serviceFilters[key];
      return !selected.length || record.facets[key].some((option) => selected.includes(option.value));
    });
  });
  const visibleServiceKeys = new Set(filteredServiceRecords.map((record) => record.key));
  const activeFilterCount = SERVICE_FACET_KEYS.reduce(
    (total, key) => total + serviceFilters[key].length,
    0,
  );
  const toggleServiceFilter = (key: ServiceFacetKey, value: string) => {
    setServiceFilters((current) => ({
      ...current,
      [key]: current[key].includes(value)
        ? current[key].filter((item) => item !== value)
        : [...current[key], value],
    }));
  };
  const resetServiceFilters = () => {
    setServiceQuery("");
    setServiceFilters(emptyServiceFilters());
  };

  return (
    <>
      <div className="section-intro">
        <div>
          <span className="eyebrow">File-backed source map</span>
          <h2>Сервисы системы</h2>
          <p>Карта быстро строится по исходникам без SSOT: точки входа, исходящие интерфейсы и предполагаемые связи сохраняются в service_map.json.</p>
        </div>
        <div className="server-summary">
          <span><b>{data.service_map.service_count}</b> сервисов</span>
          <span><b>{data.service_map.entrypoint_count}</b> входов</span>
          <span><b>{data.service_map.dependency_count}</b> связей</span>
          <span><b>{data.service_map.unresolved_dependency_count}</b> не определено</span>
        </div>
      </div>

      <div className="server-note analysis-note">
        <span>↧</span>
        <p>{data.catalog.analysis.available
          ? <>Последний полный analysis run сохранён: <code>{data.catalog.analysis.path}</code></>
          : <>Архив анализа пока пуст. Запустите анализ любого сервиса или полную пересборку графа.</>}</p>
      </div>

      <div className={`server-note analysis-note ${gigacode.available ? "" : "warning"}`}>
        <span>{gigacode.available ? "✦" : "!"}</span>
        <p>{gigacode.available
          ? <>GigaCode готов: <code>{gigacode.executable}</code> · {gigacode.version}. Кнопка на карточке сначала обновит статическую карту, затем запустит GigaCode, создаст SSOT и обновит индекс.</>
          : <>GigaCode недоступен: <code>{gigacode.error || "executable не найден"}</code>. Укажите реальный абсолютный путь в <code>KB_GIGACODE_COMMAND</code> и перезапустите backend.</>}</p>
      </div>

      {serviceFilterRecords.length > 0 && (
        <section className="service-filter-panel" aria-label="Фильтры сервисов">
          <div className="service-filter-head">
            <div><span className="eyebrow">Фильтры карточек</span><b>{number(filteredServiceRecords.length)} из {number(serviceFilterRecords.length)}</b></div>
            <label className="service-filter-search"><span>Поиск</span><input value={serviceQuery} onChange={(event) => setServiceQuery(event.target.value)} placeholder="Сервис, ID, модуль…" /></label>
            {(activeFilterCount > 0 || serviceQuery) && <button type="button" className="button quiet" onClick={resetServiceFilters}>Сбросить всё</button>}
          </div>
          <div className="service-filter-groups">
            {facetGroups.map((group) => (
              <details
                className="service-filter-group"
                key={group.key}
                onToggle={(event) => {
                  if (!event.currentTarget.open) return;
                  event.currentTarget.parentElement?.querySelectorAll("details[open]").forEach((details) => {
                    if (details !== event.currentTarget) details.removeAttribute("open");
                  });
                }}
              >
                <summary><span>{group.label}</span>{serviceFilters[group.key].length > 0 && <b>{serviceFilters[group.key].length}</b>}<i>⌄</i></summary>
                <div className="service-filter-menu">
                  {group.options.map((option) => {
                    const checked = serviceFilters[group.key].includes(option.value);
                    return (
                      <label className={checked ? "selected" : ""} key={option.value}>
                        <input type="checkbox" checked={checked} aria-label={`${group.label}: ${option.label}`} onChange={() => toggleServiceFilter(group.key, option.value)} />
                        <span>{option.label}</span><em>{number(option.count)}</em>
                      </label>
                    );
                  })}
                </div>
              </details>
            ))}
          </div>
          {activeFilterCount > 0 && (
            <div className="service-active-filters">
              {facetGroups.flatMap((group) => serviceFilters[group.key].map((value) => {
                const option = group.options.find((item) => item.value === value);
                return <button type="button" key={`${group.key}:${value}`} onClick={() => toggleServiceFilter(group.key, value)}><small>{group.label}</small>{option?.label || value}<span>×</span></button>;
              }))}
            </div>
          )}
        </section>
      )}

      {serviceFilterRecords.length ? filteredServiceRecords.length ? (
        <div className="service-grid">
          {data.catalog.repositories.flatMap((repository) => {
            const mappedServices = servicesByRepository.get(repository.checkout_path)
              || servicesByRepository.get(repository.name)
              || [];
            const repositoryIndex = data.catalog.indexes.find(
              (index) => index.id === repository.index_id,
            );
            const repositoryJobs = data.catalog.jobs.filter(
              (job) => job.type === "repository"
                && (
                  job.target_id === repository.id
                  || (!job.target_id && job.index_id === repository.index_id)
                ),
            );
            const activeRepositoryJob = data.catalog.jobs.find(
              (job) => ["queued", "running", "cancelling"].includes(job.status)
                && job.type === "repository"
                && (
                  job.target_id === repository.id
                  || (!job.target_id && job.index_id === repository.index_id)
                ),
            );
            const activeGraphJob = data.catalog.jobs.find(
              (job) => job.type === "graph"
                && ["queued", "running", "cancelling"].includes(job.status),
            );
            const latestRepositoryJob = repositoryJobs[0];
            const lastGraphJob = data.catalog.jobs.find((job) => job.type === "graph");
            const graphError = lastGraphJob?.status === "failed"
              && (!latestRepositoryJob || lastGraphJob.id > latestRepositoryJob.id)
              ? lastGraphJob.error || lastGraphJob.message
              : null;
            const repositoryError = latestRepositoryJob?.status === "failed"
              ? latestRepositoryJob.error || latestRepositoryJob.message
              : repositoryIndex?.status === "error"
                ? repositoryIndex.error || "Ошибка индексации"
                : graphError;
            if (!mappedServices.length) {
              if (!visibleServiceKeys.has(`repository:${repository.id}`)) return [];
              return [(
              <article className="service-card" key={repository.id} aria-busy={Boolean(activeRepositoryJob || activeGraphJob)}>
                <header>
                  <span className="service-mark">S</span>
                  <Status value={activeRepositoryJob || activeGraphJob ? "running" : repositoryError ? "failed" : "empty"} />
                </header>
                <span className="eyebrow">{repositoryError ? "Analysis failed · retry available" : "Awaiting analysis"}</span>
                <h3>{repository.name}</h3>
                <code>{repository.commit?.slice(0, 12) || "service pending"}</code>
                <dl>
                  <div><dt>Индекс</dt><dd>{indexNames[repository.index_id] || repository.index_id}</dd></div>
                  <div><dt>OpenSpec</dt><dd>{number(repository.document_count)} документов</dd></div>
                  <div><dt>Хранение</dt><dd>{repository.checkout_state === "removed" ? "только документация" : repository.checkout_state === "external" ? "внешний источник" : "checkout доступен"}</dd></div>
                  <div><dt>Интерфейсы</dt><dd>—</dd></div>
                  <div><dt>Синхронизация</dt><dd>{relativeDate(repository.synced_at)}</dd></div>
                  {repositoryError && <div><dt>Ошибка</dt><dd title={repositoryError}>{short(repositoryError, 100)}</dd></div>}
                </dl>
                <footer>
                  <span>{activeRepositoryJob?.message || "Владелец не определён"}</span>
                  <div className="card-actions">
                    <button
                      className="service-rebuild"
                      disabled={Boolean(activeRepositoryJob)}
                      onClick={() => void onAction(
                        () => post("/admin/api/repositories/refresh", password, { repository_id: repository.id }),
                        `OpenSpec, RAG и анализ «${repository.name}» поставлены в очередь`,
                      )}
                    >
                      {activeRepositoryJob
                        ? "◌ OpenSpec + RAG обновляются…"
                        : repositoryError
                          ? "↻ Повторить OpenSpec + RAG"
                          : "↻ OpenSpec + RAG + анализ"}
                    </button>
                    <button onClick={onGraph}>Граф</button>
                  </div>
                </footer>
              </article>
              )];
            }
            return mappedServices.filter(
              (mappedService) => visibleServiceKeys.has(`${repository.id}:${mappedService.id}`),
            ).map((mappedService) => {
              const activeServiceJob = data.catalog.jobs.find(
                (job) => job.type === "service"
                  && job.target_id === mappedService.id
                  && ["queued", "running", "cancelling"].includes(job.status),
              );
              const serviceAuthenticationUrl = jobAuthenticationUrl(activeServiceJob);
              return (
                <article
                  className="service-card"
                  key={`${repository.id}:${mappedService.id}`}
                  aria-busy={Boolean(activeRepositoryJob || activeGraphJob || activeServiceJob)}
                >
                  <header>
                    <span className="service-mark">S</span>
                    <Status value={activeRepositoryJob || activeGraphJob || activeServiceJob ? "running" : repositoryError ? "failed" : mappedService.module_state === "active" ? "ready" : mappedService.module_state === "unsupported" ? "unsupported" : "module-empty"} />
                  </header>
                  <span className="eyebrow">
                    {mappedService.module_path === "." ? "Repository service" : `Module · ${mappedService.module_path}`}
                  </span>
                  <h3>{mappedService.name}</h3>
                  <code>{mappedService.id}</code>
                  <dl>
                    <div><dt>Репозиторий</dt><dd>{repository.name}</dd></div>
                    <div><dt>Хранение</dt><dd>{repository.checkout_state === "removed" ? "только документация" : repository.checkout_state === "external" ? "внешний источник" : "checkout доступен"}</dd></div>
                    <div><dt>Build</dt><dd>{mappedService.build_system} · {mappedService.module_state}</dd></div>
                    <div><dt>Подмодули</dt><dd>{mappedService.component_paths.length ? mappedService.component_paths.join(", ") : "—"}</dd></div>
                    <div><dt>Интерфейсы</dt><dd>{mappedService.entrypoint_count} входов · {mappedService.outbound_interface_count} выходов</dd></div>
                    <div><dt>Индекс</dt><dd>{indexNames[repository.index_id] || repository.index_id}</dd></div>
                    {repositoryError && <div><dt>Ошибка</dt><dd title={repositoryError}>{short(repositoryError, 100)}</dd></div>}
                  </dl>
                  <footer>
                    <span>{activeServiceJob?.message || mappedService.owner || "Владелец не определён"}</span>
                    <div className="card-actions">
                      <button
                        className="service-rebuild"
                        disabled={Boolean(activeRepositoryJob)}
                        onClick={() => void onAction(
                          () => post("/admin/api/repositories/refresh", password, { repository_id: repository.id }),
                          `OpenSpec, RAG и анализ «${repository.name}» поставлены в очередь`,
                        )}
                      >
                        {activeRepositoryJob
                          ? "◌ OpenSpec + RAG обновляются…"
                          : repositoryError
                            ? "↻ Повторить OpenSpec + RAG"
                            : "↻ OpenSpec + RAG"}
                      </button>
                      {serviceAuthenticationUrl
                        ? <a className="service-auth-link" href={serviceAuthenticationUrl} target="_blank" rel="noreferrer">Войти в GigaCode ↗</a>
                        : <button
                            disabled={Boolean(activeServiceJob || activeGraphJob) || !gigacode.available}
                            title={gigacode.available ? "Статика → GigaCode → SSOT → RAG" : gigacode.error || "GigaCode недоступен"}
                            onClick={() => void onAction(
                              () => post("/admin/api/services/analyze", password, {
                                service_id: mappedService.id,
                                generation_mode: "gigacode",
                              }),
                              `GigaCode-анализ «${mappedService.name}» поставлен в очередь`,
                            )}
                          >
                            {activeServiceJob
                              ? "◌ GigaCode анализирует…"
                              : gigacode.available
                                ? "✦ Анализ через GigaCode"
                                : "GigaCode недоступен"}
                          </button>}
                      <button onClick={() => onSsot(mappedService, repository.index_id)}>SSOT</button>
                      <button onClick={onGraph}>Граф</button>
                      <button className="danger" onClick={() => { if (!window.confirm(`Удалить сервис «${mappedService.name}» из карты? Модуль останется в repository как постоянное исключение.`)) return; void onAction(() => post("/admin/api/services/delete", password, { service_id: mappedService.id }), "Удаление сервиса поставлено в очередь"); }}>Удалить</button>
                    </div>
                  </footer>
                </article>
              );
            });
          })}
          {standaloneMapServices.filter(
            (service) => visibleServiceKeys.has(`standalone:${service.id}`),
          ).map((service) => (
            <article className="service-card" key={service.id}>
              <header><span className="service-mark">S</span><Status value="ready" /></header>
              <span className="eyebrow">Source map discovered</span>
              <h3>{service.name}</h3>
              <code>{service.id}</code>
              <dl><div><dt>Репозиторий</dt><dd>{service.repository || "—"}</dd></div><div><dt>Интерфейсы</dt><dd>{service.entrypoint_count} входов · {service.outbound_interface_count} выходов</dd></div></dl>
              <footer><span>{service.owner || "Владелец не определён"}</span><button onClick={onGraph}>Открыть граф →</button></footer>
            </article>
          ))}
        </div>
      ) : (
        <div className="empty-state services-empty"><div>⌁</div><h3>По фильтрам ничего не найдено</h3><p>Выберите другие значения или сбросьте активные фильтры.</p><button className="button secondary" onClick={resetServiceFilters}>Сбросить фильтры</button></div>
      ) : (
        <div className="empty-state services-empty"><div>▦</div><h3>Сервисов пока нет</h3><p>Подключите Git-репозиторий. Сервис появится после быстрого анализа исходников, даже без SSOT.</p></div>
      )}
    </>
  );
}

function ServersPage({ data, password, onAction }: {
  data: Overview;
  password: string;
  onAction: (run: () => Promise<unknown>, message: string) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState<string | null>("local");
  const servers = data.mcp_servers.servers;
  const totalTools = servers.reduce((sum, server) => sum + server.tool_count, 0);

  return (
    <>
      <div className="section-intro">
        <div>
          <span className="eyebrow">MCP topology</span>
          <h2>Подключённые MCP-серверы</h2>
          <p>Здесь виден локальный RAG MCP endpoint и внешние Streamable HTTP серверы. Проверка выполняет настоящее MCP discovery и сохраняет найденные tools.</p>
        </div>
        <div className="server-summary">
          <span><b>{data.mcp_servers.online_count}</b> в сети</span>
          <span><b>{data.mcp_servers.server_count}</b> всего</span>
          <span><b>{totalTools}</b> tools</span>
        </div>
      </div>

      <div className="server-grid">
        {servers.map((server) => (
          <ServerCard
            key={server.id}
            server={server}
            expanded={expanded === server.id}
            onToggle={() => setExpanded((current) => current === server.id ? null : server.id)}
            onCheck={() => void onAction(
              () => post("/admin/api/mcp-servers/check", password, { id: server.id }),
              `${server.name}: discovery завершён`,
            )}
            onDelete={() => {
              if (!window.confirm(`Удалить MCP server «${server.name}» из реестра?`)) return;
              void onAction(
                () => post("/admin/api/mcp-servers/delete", password, { id: server.id }),
                "MCP server удалён",
              );
            }}
          />
        ))}
      </div>

      <div className="server-note">
        <span>i</span>
        <p>Реестр хранит только адреса серверов. Секреты в URL не сохраняются; для защищённого endpoint используйте клиентский proxy.</p>
      </div>
    </>
  );
}

function ServerCard({ server, expanded, onToggle, onCheck, onDelete }: {
  server: McpServer;
  expanded: boolean;
  onToggle: () => void;
  onCheck: () => void;
  onDelete: () => void;
}) {
  return (
    <article className={`server-card ${server.kind === "local" ? "local" : ""}`}>
      <header>
        <div className="server-mark">{server.kind === "local" ? "R" : "M"}</div>
        <div className="grow">
          <div className="server-title"><h3>{server.name}</h3><span className="tag">{server.kind === "local" ? "LOCAL" : "EXTERNAL"}</span></div>
          <code title={server.url}>{server.url}</code>
        </div>
        <Status value={server.status} />
      </header>

      <div className="server-facts">
        <span><b>{server.tool_count}</b> MCP tools</span>
        <span><b>HTTP</b> transport</span>
        <span><b>{relativeDate(server.checked_at)}</b> проверка</span>
      </div>

      {server.error && <div className="server-error">{server.error}</div>}

      {expanded && (
        <div className="server-tools">
          <div className="server-tools-head"><span>DISCOVERED TOOLS</span><b>{server.tool_count}</b></div>
          {server.tools.length ? server.tools.map((tool) => (
            <div className="server-tool-row" key={tool.name}>
              <span className="tool-dot">⌁</span>
              <div className="grow"><code>{tool.name}</code><small>{tool.description || "Описание не предоставлено сервером"}</small></div>
              {tool.kind && <span className={`tag ${tool.kind === "managed" ? "managed" : ""}`}>{tool.kind}</span>}
            </div>
          )) : <div className="empty-tools">Tools не найдены. Запустите проверку endpoint.</div>}
        </div>
      )}

      <footer>
        <button onClick={onToggle}>{expanded ? "Скрыть tools" : "Показать tools"} {expanded ? "↑" : "↓"}</button>
        <div>
          <button onClick={onCheck}>↻ Проверить</button>
          {server.deletable && <button className="danger" onClick={onDelete}>Удалить</button>}
        </div>
      </footer>
    </article>
  );
}

type PlaygroundValue = string | boolean;

interface ToolTestResponse {
  tool: string;
  elapsed_ms: number;
  content: unknown[];
  structured_content: Record<string, unknown> | null;
  meta: Record<string, unknown> | null;
  is_error: boolean;
}

function schemaProperties(schema: Record<string, unknown>): Record<string, Record<string, unknown>> {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return {};
  return properties as Record<string, Record<string, unknown>>;
}

function schemaType(property: Record<string, unknown>): string {
  if (typeof property.type === "string") return property.type;
  if (Array.isArray(property.anyOf)) {
    const concrete = property.anyOf.find(
      (item) => typeof item === "object" && item !== null && (item as { type?: unknown }).type !== "null",
    );
    if (concrete && typeof (concrete as { type?: unknown }).type === "string") {
      return String((concrete as { type: string }).type);
    }
  }
  return "string";
}

function initialPlaygroundValues(tool: ToolCatalogItem): Record<string, PlaygroundValue> {
  const samples: Record<string, string> = {
    feature: "Добавить резервирование товара при создании заказа",
    question: "Как устроено текущее состояние системы?",
    query: "Как устроено текущее состояние системы?",
  };
  return Object.fromEntries(
    Object.entries(schemaProperties(tool.input_schema)).map(([name, property]) => {
      const value = property.default ?? samples[name] ?? (schemaType(property) === "boolean" ? false : "");
      return [name, typeof value === "boolean" ? value : String(value ?? "")];
    }),
  );
}

function ToolPlayground({ tool, password }: { tool: ToolCatalogItem; password: string }) {
  const [values, setValues] = useState<Record<string, PlaygroundValue>>(() => initialPlaygroundValues(tool));
  const [result, setResult] = useState<ToolTestResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const properties = schemaProperties(tool.input_schema);
  const required = new Set(Array.isArray(tool.input_schema.required) ? tool.input_schema.required.map(String) : []);

  const run = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const argumentsPayload: Record<string, unknown> = {};
      for (const [name, property] of Object.entries(properties)) {
        const raw = values[name];
        const type = schemaType(property);
        if ((raw === "" || raw === undefined) && !required.has(name)) continue;
        if ((raw === "" || raw === undefined) && required.has(name)) {
          throw new Error(`Заполните обязательный параметр ${name}`);
        }
        if (type === "integer") argumentsPayload[name] = Number.parseInt(String(raw), 10);
        else if (type === "number") argumentsPayload[name] = Number(raw);
        else if (type === "boolean") argumentsPayload[name] = Boolean(raw);
        else if (type === "array" || type === "object") argumentsPayload[name] = JSON.parse(String(raw));
        else argumentsPayload[name] = String(raw);
      }
      const payload = await post<ToolTestResponse>(
        "/admin/api/tools/test",
        password,
        { name: tool.name, arguments: argumentsPayload },
        120_000,
      );
      setResult(payload);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Tool завершился с ошибкой");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="tool-playground" onSubmit={run}>
      <div className="playground-head"><span className="eyebrow">Live FastMCP call</span><b>Параметры из JSON Schema</b></div>
      {Object.entries(properties).map(([name, property]) => {
        const type = schemaType(property);
        const description = typeof property.description === "string" ? property.description : "";
        const isLongText = ["feature", "question", "query"].includes(name);
        return (
          <label className="playground-field" key={name}>
            <span><code>{name}</code>{required.has(name) && <i>обязательно</i>}</span>
            {type === "boolean" ? (
              <input type="checkbox" checked={Boolean(values[name])} onChange={(event) => setValues((current) => ({ ...current, [name]: event.target.checked }))} />
            ) : isLongText ? (
              <textarea value={String(values[name] ?? "")} onChange={(event) => setValues((current) => ({ ...current, [name]: event.target.value }))} />
            ) : (
              <input
                type={name.toLowerCase().includes("password") ? "password" : ["integer", "number"].includes(type) ? "number" : "text"}
                min={typeof property.minimum === "number" ? property.minimum : undefined}
                max={typeof property.maximum === "number" ? property.maximum : undefined}
                value={String(values[name] ?? "")}
                onChange={(event) => setValues((current) => ({ ...current, [name]: event.target.value }))}
              />
            )}
            {description && <small>{description}</small>}
          </label>
        );
      })}
      {!Object.keys(properties).length && <div className="callout">У этого tool нет входных параметров.</div>}
      <button className="button primary playground-run" disabled={busy}>{busy ? "Выполняется…" : "▶ Вызвать tool"}</button>
      {error && <div className="form-error">{error}</div>}
      {result && (
        <div className="playground-result">
          <div><span className={result.is_error ? "result-error" : "result-ok"}>{result.is_error ? "ERROR" : "SUCCESS"}</span><b>{result.elapsed_ms} ms</b></div>
          <pre>{JSON.stringify(result, null, 2)}</pre>
        </div>
      )}
    </form>
  );
}

function ToolsPage({ data, password, onEditManaged, onEditBuiltin, onAction }: {
  data: Overview;
  password: string;
  onEditManaged: (tool: ManagedTool) => void;
  onEditBuiltin: (tool: ToolCatalogItem) => void;
  onAction: (run: () => Promise<unknown>, message: string) => Promise<void>;
}) {
  const [testing, setTesting] = useState<string | null>(null);
  const indexNames = Object.fromEntries(data.catalog.indexes.map((index) => [index.id, index.name]));
  const managedByName = Object.fromEntries(data.managed_tools.tools.map((tool) => [tool.name, tool]));
  const catalog = data.tool_catalog?.tools ?? [];
  return (
    <>
      <div className="section-intro"><div><span className="eyebrow">Live tools/list catalog</span><h2>Все MCP tools</h2><p>Встроенные и управляемые инструменты с реальной JSON Schema. Описание определяет, когда нейросеть выберет tool; playground показывает точный ответ FastMCP.</p></div><div className="server-summary"><span><b>{data.tool_catalog?.built_in_count ?? 0}</b> встроенных</span><span><b>{data.tool_catalog?.managed_count ?? 0}</b> управляемых</span></div></div>
      <div className="tool-grid">
        {catalog.map((tool) => {
          const managed = managedByName[tool.name];
          const propertyCount = Object.keys(schemaProperties(tool.input_schema)).length;
          return (
          <article className="tool-card" key={tool.name}>
            <header><span className="tool-mark">{tool.name === "kb_system_graph" ? "⌘" : tool.name === "kb_feature_context" ? "◇" : "⌁"}</span><span className={`tag ${tool.kind === "managed" ? "managed" : ""}`}>{tool.kind}</span><button className="dots" aria-label={`Настроить ${tool.name}`} onClick={() => managed ? onEditManaged(managed) : onEditBuiltin(tool)}>•••</button></header>
            <code>{tool.name}</code><p>{tool.description}</p>
            <div className="bindings">{tool.kind === "managed" ? (tool.index_ids.length ? tool.index_ids.map((id) => <span className="tag" key={id}>◇ {indexNames[id] || id}</span>) : <span className="tag warning">Не привязан</span>) : <><span className="tag">code-backed</span>{tool.description_overridden && <span className="tag managed">описание изменено</span>}</>}</div>
            <footer><span>{propertyCount} параметров · JSON Schema</span><div className="card-actions"><button onClick={() => setTesting(testing === tool.name ? null : tool.name)}>{testing === tool.name ? "Скрыть тест ↑" : "Проверить →"}</button><button onClick={() => managed ? onEditManaged(managed) : onEditBuiltin(tool)}>Изменить</button>{managed && <button className="danger" onClick={() => { if (!window.confirm(`Удалить MCP tool «${tool.name}»?`)) return; void onAction(() => post("/admin/api/tools/delete", password, { name: tool.name }), "Tool удалён"); }}>Удалить</button>}</div></footer>
            {testing === tool.name && <ToolPlayground key={tool.name} tool={tool} password={password} />}
          </article>
          );
        })}
        <button className="new-tool-card" onClick={() => document.querySelector<HTMLButtonElement>(".top-actions .primary")?.click()}><span>＋</span><b>Создать поисковый MCP tool</b><small>Выберите индексы и опишите агенту назначение поиска</small></button>
      </div>
      <div className="danger-note"><span>i</span><p>У встроенных tools редактируется описание для LLM, а исполняемый код и JSON Schema остаются защищёнными. После изменения описания уже подключённому MCP-клиенту может потребоваться переподключение, чтобы повторить tools/list.</p></div>
    </>
  );
}

function IndexForm({ password, onClose, onSaved }: { password: string; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try { await post("/admin/api/indexes", password, { name, description }); onSaved(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Не удалось создать индекс"); }
  };
  return <Modal title="Новый индекс" onClose={onClose}><form className="modal-form" onSubmit={submit}><label>Название<input required minLength={2} value={name} onChange={(event) => setName(event.target.value)} placeholder="Architecture decisions" /></label><label>Описание<textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Какой слой знаний хранится в этом индексе" /></label><div className="callout">Индекс создаётся пустым. Добавьте Git/OpenSpec-источник или загрузите документы через API.</div>{error && <div className="form-error">{error}</div>}<div className="modal-actions"><button type="button" className="button quiet" onClick={onClose}>Отмена</button><button className="button primary">Создать индекс</button></div></form></Modal>;
}

function ServerForm({ password, onClose, onSaved }: { password: string; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    try {
      await post("/admin/api/mcp-servers", password, { name, url });
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось добавить MCP server");
      setSaving(false);
    }
  };
  return (
    <Modal title="Добавить MCP server" onClose={onClose}>
      <form className="modal-form" onSubmit={submit}>
        <label>Название<input required minLength={2} value={name} onChange={(event) => setName(event.target.value)} placeholder="architecture-mcp" /></label>
        <label>Streamable HTTP endpoint<input required type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="http://127.0.0.1:9000/mcp" /><small>Укажите полный URL MCP endpoint, включая путь `/mcp`.</small></label>
        <div className="flow-preview"><span>RAG Control Plane</span><i>→</i><span>MCP initialize</span><i>→</i><span>tools/list</span></div>
        <div className="callout">После сохранения сервер сразу проверяется. Даже недоступный endpoint останется в реестре со статусом «не в сети».</div>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="button quiet" onClick={onClose}>Отмена</button><button className="button primary" disabled={saving}>{saving ? "Проверяем…" : "Добавить и проверить"}</button></div>
      </form>
    </Modal>
  );
}

function RepositoryForm({ password, indexes, repositories, gigacodeAvailable, gigacodeError, onClose, onSaved, onBatchStarted }: {
  password: string;
  indexes: RagIndex[];
  repositories: RepositorySource[];
  gigacodeAvailable: boolean;
  gigacodeError: string | null;
  onClose: () => void;
  onSaved: (mode: "static" | "gigacode") => void;
  onBatchStarted: (
    count: number,
    scheduled: number,
    skipped: number,
    workers: number,
    mode: "static" | "gigacode",
    fallbackReason: string | null,
  ) => void;
}) {
  const [formMode, setFormMode] = useState<"single" | "batch">("single");
  const [name, setName] = useState("");
  const [nameEdited, setNameEdited] = useState(false);
  const [gitUrl, setGitUrl] = useState("");
  const [ref, setRef] = useState("master");
  const [target, setTarget] = useState(indexes[0]?.id || "__new__");
  const [indexName, setIndexName] = useState("");
  const [error, setError] = useState("");
  const [busyMode, setBusyMode] = useState<"static" | "gigacode" | null>(null);
  const [csvFilename, setCsvFilename] = useState("");
  const [batchRows, setBatchRows] = useState<RepositoryBatchRow[]>([]);
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchDefaultBranch, setBatchDefaultBranch] = useState("master");
  const [batchDefaultIndexId, setBatchDefaultIndexId] = useState(indexes[0]?.id || "");
  const [workerCount, setWorkerCount] = useState(4);

  const loadCsv = async (file: File | undefined) => {
    if (!file) return;
    setError("");
    try {
      const rows = parseRepositoryCsv(await file.text(), indexes, repositories);
      setBatchRows(rows);
      setCsvFilename(file.name);
    } catch (caught) {
      setBatchRows([]);
      setCsvFilename("");
      setError(caught instanceof Error ? caught.message : "Не удалось прочитать CSV");
    }
  };

  const runBatch = async () => {
    if (!batchRows.length || batchRunning) return;
    const defaultBranch = batchDefaultBranch.trim();
    if (!defaultBranch) {
      setError("Укажите ветку по умолчанию");
      return;
    }
    if (!batchDefaultIndexId) {
      setError("Выберите индекс по умолчанию");
      return;
    }
    const rows = batchRows.slice();
    const activeWorkerCount = Math.min(Math.max(1, Math.trunc(workerCount)), rows.length);
    setBatchRunning(true);
    setError("");
    try {
      const job = await post<RepositoryBatchJobResponse>(
        "/admin/api/repositories/batch",
        password,
        {
          repositories: rows.map((row) => ({
            name: row.name,
            git_url: row.gitUrl,
            ref: row.branch || defaultBranch,
            index_id: row.indexId || batchDefaultIndexId,
          })),
          worker_count: activeWorkerCount,
          prefer_gigacode: true,
        },
        30000,
      );
      onBatchStarted(
        job.repository_count,
        job.scheduled_count,
        job.skipped_count,
        job.worker_count,
        job.generation_mode,
        job.fallback_reason,
      );
    } catch (caught) {
      setBatchRunning(false);
      setError(caught instanceof Error ? caught.message : "Не удалось запустить пакетный импорт");
    }
  };

  const connect = async (generationMode: "static" | "gigacode") => {
    if (duplicateRepository) {
      setError(`Git-репозиторий уже подключён: ${duplicateRepository.name}`);
      return;
    }
    setBusyMode(generationMode);
    setError("");
    try {
      await post("/admin/api/repositories", password, {
        name,
        git_url: gitUrl,
        ref: ref || null,
        index_id: target === "__new__" ? null : target,
        index_name: target === "__new__" ? indexName || name : null,
        generation_mode: generationMode,
      });
      onSaved(generationMode);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось подключить репозиторий");
      setBusyMode(null);
    }
  };
  const submitStatic = (event: FormEvent) => {
    event.preventDefault();
    void connect("static");
  };
  const submitGigacode = (event: React.MouseEvent<HTMLButtonElement>) => {
    const form = event.currentTarget.form;
    if (!form?.reportValidity()) return;
    void connect("gigacode");
  };
  const changeGitUrl = (value: string) => {
    setGitUrl(value);
    if (!nameEdited) setName(repositoryNameFromGitUrl(value));
  };
  const duplicateRepository = gitUrl.trim()
    ? repositories.find(
      (repository) => repositorySourceKey(repository.git_url) === repositorySourceKey(gitUrl),
    ) || null
    : null;
  const skippedBatchCount = batchRows.filter((row) => row.skipReason !== null).length;
  const scheduledBatchCount = batchRows.length - skippedBatchCount;
  return (
    <Modal title="Подключить Git-репозитории" onClose={onClose} closeDisabled={busyMode !== null || batchRunning} className="repository-modal">
      <div className="repository-mode-tabs" role="tablist" aria-label="Режим подключения">
        <button type="button" className={formMode === "single" ? "active" : ""} onClick={() => { setFormMode("single"); setError(""); }} disabled={batchRunning}>Один репозиторий</button>
        <button type="button" className={formMode === "batch" ? "active" : ""} onClick={() => { setFormMode("batch"); setError(""); }} disabled={busyMode !== null}>CSV-пакет</button>
      </div>
      {formMode === "single" ? (
        <form className="modal-form repository-single-form" onSubmit={submitStatic}>
          <div className="field-row">
            <label>Название<input required minLength={2} value={name} onChange={(event) => { setName(event.target.value); setNameEdited(true); }} placeholder="Заполнится из Git URL" /></label>
            <label>Ref<input value={ref} onChange={(event) => setRef(event.target.value)} placeholder="master" /></label>
          </div>
          <label>Git URL<input required value={gitUrl} onChange={(event) => changeGitUrl(event.target.value)} placeholder="https://git.company.local/team/service.git" /></label>
          {duplicateRepository && <div className="duplicate-repository-hint">Этот Git-репозиторий уже подключён: <b>{duplicateRepository.name}</b>. Повторное сканирование не будет запущено.</div>}
          <label>Целевой индекс<select value={target} onChange={(event) => setTarget(event.target.value)}>{indexes.map((index) => <option key={index.id} value={index.id}>{index.name}</option>)}<option value="__new__">＋ Создать новый индекс</option></select></label>
          {target === "__new__" && <label>Название нового индекса<input value={indexName} onChange={(event) => setIndexName(event.target.value)} placeholder={name || "System knowledge"} /></label>}
          {!gigacodeAvailable && <small className="gigacode-hint">GigaCode-режим недоступен: {gigacodeError || "исполняемый файл не найден"}</small>}
          {error && <div className="form-error">{error}</div>}
          <div className="modal-actions repository-actions">
            <button type="button" className="button quiet" onClick={onClose} disabled={busyMode !== null}>Отмена</button>
            <button type="submit" className="button secondary" disabled={busyMode !== null || duplicateRepository !== null}>{busyMode === "static" ? "Подключаем…" : "Без GigaCode"}</button>
            <button type="button" className="button precise" onClick={submitGigacode} disabled={!gigacodeAvailable || busyMode !== null || duplicateRepository !== null} title={!gigacodeAvailable ? gigacodeError || "GigaCode недоступен" : duplicateRepository ? "Репозиторий уже подключён" : "Создать SSOT через GigaCode и собрать индекс"}>{busyMode === "gigacode" ? "Запускаем GigaCode…" : "✦ С GigaCode"}</button>
          </div>
        </form>
      ) : (
        <section className="modal-form repository-batch">
          <div className="batch-defaults">
            <label>
              Ветка для всех репозиториев
              <input required value={batchDefaultBranch} disabled={batchRunning} onChange={(event) => setBatchDefaultBranch(event.target.value)} placeholder="master" />
              <small>Используется, если в строке CSV поле <code>branch</code> пустое.</small>
            </label>
            <label>
              Индекс для всех репозиториев
              <select required value={batchDefaultIndexId} disabled={batchRunning} onChange={(event) => setBatchDefaultIndexId(event.target.value)}>
                {indexes.map((index) => <option key={index.id} value={index.id}>{index.name} · {index.id}</option>)}
              </select>
              <small>Используется, если в строке CSV поле <code>index</code> пустое.</small>
            </label>
            <p>Значения <code>branch</code> и <code>index</code> из конкретной строки CSV переопределяют эти настройки.</p>
          </div>
          <div className="batch-worker-control">
            <div><b>Параллельная подготовка</b><small>Воркеры одновременно клонируют и сканируют репозитории. После этого сервер один раз собирает общий граф и каждый затронутый индекс.</small></div>
            <label>Количество воркеров<input type="number" min={1} max={16} step={1} value={workerCount} disabled={batchRunning} onChange={(event) => setWorkerCount(Math.min(16, Math.max(1, Math.trunc(Number(event.target.value) || 1))))} /></label>
          </div>
          {!batchRows.length && (
            <label className="csv-drop">
              <input type="file" accept=".csv,text/csv" onChange={(event) => void loadCsv(event.target.files?.[0])} />
              <span className="drop-icon">⇧</span>
              <b>Загрузить CSV со списком репозиториев</b>
              <small>До 1000 строк. Колонки: <strong>git</strong> — обязательная, <strong>branch</strong> и <strong>index</strong> — необязательные.</small>
              <code>git,branch,index<br />https://git.company.local/team/payments.git,develop,corporate-knowledge</code>
              <small className="csv-index-hint"><code>index</code> принимает ID или точное название существующего индекса.</small>
            </label>
          )}
          {batchRows.length > 0 && (
            <>
              <div className="batch-head">
                <div><b>{csvFilename}</b><small>{scheduledBatchCount} в обработку · {skippedBatchCount} пропустить</small></div>
                <label className="button quiet batch-replace">Заменить CSV<input type="file" accept=".csv,text/csv" disabled={batchRunning} onChange={(event) => void loadCsv(event.target.files?.[0])} /></label>
              </div>
              <div className="batch-queue">
                {batchRows.map((row, index) => (
                  <article key={row.id} className={`batch-row batch-row-${row.skipReason ? "skipped" : "pending"}`}>
                    <span className="batch-number">{index + 1}</span>
                    <div className="batch-repository"><b>{row.name}</b><small title={row.gitUrl}>{row.gitUrl}</small><em>{row.skipReason || `${row.branch || batchDefaultBranch} · ${indexes.find((item) => item.id === (row.indexId || batchDefaultIndexId))?.name || row.indexId || batchDefaultIndexId}`}</em></div>
                    <div className="batch-state"><Status value={row.skipReason ? "skipped" : "pending"} /></div>
                  </article>
                ))}
              </div>
            </>
          )}
          {!gigacodeAvailable && <small className="gigacode-hint">Сейчас GigaCode недоступен ({gigacodeError || "исполняемый файл не найден"}), поэтому сервер запустит пакет со статическим анализом. Доступность перепроверяется при запуске пакета.</small>}
          {error && <div className="form-error">{error}</div>}
          <div className="modal-actions repository-actions">
            <button type="button" className="button quiet" onClick={onClose} disabled={batchRunning}>Отмена</button>
            <button type="button" className="button primary" onClick={() => void runBatch()} disabled={!batchRows.length || batchRunning}>{batchRunning ? "Проверяем и ставим в очередь…" : !batchRows.length ? "Сначала загрузите CSV" : scheduledBatchCount > 0 ? `Подключить ${scheduledBatchCount} · пропустить ${skippedBatchCount}` : `Пропустить все ${skippedBatchCount}`}</button>
          </div>
        </section>
      )}
    </Modal>
  );
}

function SystemSsotModal({ password, indexes, generator, serviceCount, onClose, onStarted }: {
  password: string;
  indexes: RagIndex[];
  generator: Overview["catalog"]["ssot_generation"];
  serviceCount: number;
  onClose: () => void;
  onStarted: () => void;
}) {
  const [indexId, setIndexId] = useState(indexes[0]?.id || "default");
  const [refreshAnalysis, setRefreshAnalysis] = useState(true);
  const [generationMode, setGenerationMode] = useState<"client" | "gigacode">(
    generator.gigacode.available ? "gigacode" : "client",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await post("/admin/api/analysis/ssot-generate", password, {
        action: "prepare",
        index_id: indexId,
        all_services: true,
        refresh_analysis: refreshAnalysis,
        generation_mode: generationMode,
      });
      onStarted();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось подготовить контекст SSOT");
      setBusy(false);
    }
  };
  return (
    <Modal title="Подготовить контекст SSOT всей системы" onClose={onClose}>
      <form className="modal-form" onSubmit={submit}>
        <div className="callout">Отдельный LLM URL серверу не нужен. Backend запускает установленный GigaCode в JSON-режиме. Если требуется вход, операция покажет кнопку со ссылкой: открой её в своём браузере, заверши авторизацию — и тот же worker продолжит read-only анализ, создаст SSOT и обновит индекс.</div>
        <label>Режим генерации<select value={generationMode} onChange={(event) => setGenerationMode(event.target.value as "client" | "gigacode")}><option value="gigacode" disabled={!generator.gigacode.available}>GigaCode на сервере{generator.gigacode.available ? ` · ${generator.gigacode.version || "готов"}` : " · недоступен"}</option><option value="client">Нейронка в GigaCode-клиенте</option></select><small>{generationMode === "gigacode" ? "GigaCode сам читает выбранные repositories, создаёт SSOT и перестраивает RAG." : "Сервер подготовит context/read_file сессию для клиентской модели."}</small></label>
        {!generator.gigacode.available && <div className="form-error">GigaCode на сервере не готов: {generator.gigacode.error}</div>}
        <label>Индекс для сгенерированного SSOT<select value={indexId} onChange={(event) => setIndexId(event.target.value)}>{indexes.map((index) => <option key={index.id} value={index.id}>{index.name} · {index.document_count} документов</option>)}</select></label>
        <label className="check"><input type="checkbox" checked={refreshAnalysis} onChange={(event) => setRefreshAnalysis(event.target.checked)} /><span>⌘</span><div><b>Сначала заново проанализировать исходники</b><small>Рекомендуется: SSOT строится из актуальной карты API и функций.</small></div></label>
        <div className="flow-preview"><span>{serviceCount} сервисов</span><i>→</i><span>{generationMode === "gigacode" ? "GigaCode JSON" : generator.provider}</span><i>→</i><span>{indexes.find((index) => index.id === indexId)?.name || indexId}</span></div>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="button quiet" onClick={onClose}>Отмена</button><button className="button primary" disabled={busy || !indexes.length}>{busy ? "Ставим в очередь…" : generationMode === "gigacode" ? "Анализировать через GigaCode" : "Подготовить контекст"}</button></div>
      </form>
    </Modal>
  );
}

function SsotModal({ password, service, indexes, defaultIndexId, onClose, onImported }: {
  password: string;
  service: Overview["service_map"]["services"][number];
  indexes: RagIndex[];
  defaultIndexId: string;
  onClose: () => void;
  onImported: () => void;
}) {
  const [indexId, setIndexId] = useState(defaultIndexId || indexes[0]?.id || "default");
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"bundle" | "import" | null>(null);

  const buildBundle = async () => {
    setBusy("bundle");
    setError("");
    try {
      const bundle = await post<{ bundle_id: string; download_url: string }>(
        "/admin/api/analysis/ssot-bundle",
        password,
        { service_id: service.id },
      );
      await download(bundle.download_url, password, `${service.id}-ssot-bundle.zip`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось подготовить SSOT-пакет");
    } finally {
      setBusy(null);
    }
  };

  const importSsot = async (event: FormEvent) => {
    event.preventDefault();
    setBusy("import");
    setError("");
    try {
      await post("/admin/api/analysis/ssot-import", password, {
        service_id: service.id,
        index_id: indexId,
        content,
      });
      onImported();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось сохранить SSOT");
      setBusy(null);
    }
  };

  return (
    <Modal title={`SSOT · ${service.name}`} onClose={onClose}>
      <form className="modal-form" onSubmit={importSsot}>
        <div className="callout">Скачайте пакет с полным analysis JSON, срезом сервиса и skill-инструкцией. Передайте ZIP нейросети, получите `ssot.md`, проверьте его и вставьте результат ниже.</div>
        <button type="button" className="button secondary" disabled={busy !== null} onClick={() => void buildBundle()}>{busy === "bundle" ? "Собираем пакет…" : "↓ Скачать пакет для нейросети"}</button>
        <label>Индекс для готового SSOT<select value={indexId} onChange={(event) => setIndexId(event.target.value)}>{indexes.map((index) => <option key={index.id} value={index.id}>{index.name}</option>)}</select></label>
        <label>Готовый SSOT Markdown<textarea className="ssot-editor" required minLength={100} value={content} onChange={(event) => setContent(event.target.value)} placeholder="---\nservice: ...\ndocument_type: ssot\n---\n\n# Service…" /><small>Документ сохранится в `knowledge/ssot/` выбранного индекса, после чего RAG автоматически пересоберётся.</small></label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="button quiet" onClick={onClose}>Закрыть</button><button className="button primary" disabled={busy !== null || content.trim().length < 100}>{busy === "import" ? "Индексируем…" : "Сохранить в индекс"}</button></div>
      </form>
    </Modal>
  );
}

function BuiltinToolForm({ password, tool, onClose, onSaved }: { password: string; tool: ToolCatalogItem; onClose: () => void; onSaved: () => void }) {
  const [description, setDescription] = useState(tool.description);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await post("/admin/api/tools/builtin", password, { name: tool.name, description });
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось обновить tool");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={`Встроенный tool · ${tool.name}`} onClose={onClose}>
      <form className="modal-form" onSubmit={submit}>
        <div className="callout">Описание попадает в MCP tools/list и объясняет нейросети, когда выбирать этот tool. Код и входная схема защищены, чтобы сохранённое изменение не могло сломать сервер.</div>
        <label>Описание для LLM<textarea required minLength={10} maxLength={4000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
        <label>Input JSON Schema — только чтение<pre className="schema-preview">{JSON.stringify(tool.input_schema, null, 2)}</pre></label>
        {error && <div className="form-error">{error}</div>}
        <div className="modal-actions"><button type="button" className="button quiet" onClick={onClose}>Отмена</button><button className="button primary" disabled={busy}>{busy ? "Сохраняем…" : "Сохранить описание"}</button></div>
      </form>
    </Modal>
  );
}

function ToolForm({ password, indexes, tool, onClose, onSaved }: { password: string; indexes: RagIndex[]; tool: ManagedTool | null; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(tool?.name || "kb_search_");
  const [description, setDescription] = useState(tool?.description || "");
  const [selected, setSelected] = useState<string[]>(tool?.index_ids || [indexes[0]?.id].filter(Boolean));
  const [topK, setTopK] = useState(tool?.defaults.top_k || 3);
  const [status, setStatus] = useState(tool?.defaults.status || "current");
  const [service, setService] = useState(tool?.defaults.service || "");
  const [domain, setDomain] = useState(tool?.defaults.domain || "");
  const [documentType, setDocumentType] = useState(tool?.defaults.document_type || "");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post("/admin/api/tools", password, { name, description, input_schema: TOOL_SCHEMA, index_ids: selected, defaults: { top_k: topK, status: status || null, service: service || null, domain: domain || null, document_type: documentType || null } });
      onSaved();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Не удалось сохранить tool"); }
  };
  return <Modal title={tool ? "Настроить MCP tool" : "Новый MCP tool"} onClose={onClose}><form className="modal-form" onSubmit={submit}><label>Имя tool<input required value={name} readOnly={Boolean(tool)} onChange={(event) => setName(event.target.value)} placeholder="kb_search_payments" /><small>Только A–Z, 0–9, _, . и -. Имя начинается с kb_.</small></label><label>Описание для агента<textarea required minLength={10} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Когда и для каких вопросов агент должен использовать этот поиск" /></label><fieldset><legend>Привязанные индексы</legend><div className="check-grid">{indexes.map((index) => <label className="check" key={index.id}><input type="checkbox" checked={selected.includes(index.id)} onChange={() => setSelected((current) => current.includes(index.id) ? current.filter((id) => id !== index.id) : [...current, index.id])} /><span>◇</span><div><b>{index.name}</b><small>{index.document_count} документов</small></div></label>)}</div><small>Можно сохранить tool без индекса — он останется видимым, но не будет выполнять поиск.</small></fieldset><div className="field-row three"><label>Top K<input type="number" min={1} max={20} value={topK} onChange={(event) => setTopK(Number(event.target.value))} /></label><label>Статус<input value={status} onChange={(event) => setStatus(event.target.value)} /></label><label>Тип документа<input value={documentType} onChange={(event) => setDocumentType(event.target.value)} placeholder="service / adr" /></label></div><div className="field-row"><label>Service filter<input value={service} onChange={(event) => setService(event.target.value)} placeholder="не задан" /></label><label>Domain filter<input value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="не задан" /></label></div>{error && <div className="form-error">{error}</div>}<div className="modal-actions"><button type="button" className="button quiet" onClick={onClose}>Отмена</button><button className="button primary">Сохранить tool</button></div></form></Modal>;
}

type ConnectivityFilter = "all" | "connected" | "incoming" | "outgoing" | "bidirectional" | "isolated";
type FocusDirection = "all" | "incoming" | "outgoing" | "both";

const CONFIDENCE_ORDER = ["DECLARED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"] as const;
const CONFIDENCE_LABELS: Record<string, string> = {
  DECLARED: "Декларативно",
  HIGH: "Подтверждено",
  MEDIUM: "Вероятно",
  LOW: "Сомнительно",
  UNRESOLVED: "Не определено",
};

function GraphPage({ data, password, gigacodeAvailable, gigacodeError, onAction }: {
  data: GraphOverview;
  password: string;
  gigacodeAvailable: boolean;
  gigacodeError: string | null;
  onAction: (run: () => Promise<unknown>, message: string) => Promise<void>;
}) {
  const [view, setView] = useState<"services" | "full">("services");
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [error, setError] = useState("");
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(
    () => new Set(Object.keys(data.nodes_by_type)),
  );
  const [selectedConfidence, setSelectedConfidence] = useState<Set<string>>(
    () => new Set(CONFIDENCE_ORDER),
  );
  const [connectivity, setConnectivity] = useState<ConnectivityFilter>("all");
  const [focusDirection, setFocusDirection] = useState<FocusDirection>("all");
  const [query, setQuery] = useState("");
  const availableTypesKey = Object.keys(data.nodes_by_type).sort().join("\u0000");
  useEffect(() => {
    api<GraphPayload>(`/admin/api/graph?view=${view}&limit=${view === "services" ? 5000 : 10000}`, password)
      .then((payload) => { setGraph(canonicalizeGraph(payload)); setSelected(null); setFocusDirection("all"); setError(""); })
      .catch((caught) => setError(caught instanceof Error ? caught.message : "Граф недоступен"));
  }, [password, view, data.generated_at]);
  useEffect(() => {
    setSelectedTypes((current) => {
      const next = new Set(current);
      Object.keys(data.nodes_by_type).forEach((type) => next.add(type));
      return next;
    });
  }, [availableTypesKey]);

  const filtered = useMemo(() => filterGraph(
    graph,
    selectedTypes,
    selectedConfidence,
    connectivity,
    selected?.id || null,
    focusDirection,
    query,
  ), [graph, selectedTypes, selectedConfidence, connectivity, selected?.id, focusDirection, query]);
  const visibleTypeCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    filtered?.nodes.forEach((node) => { counts[node.type] = (counts[node.type] || 0) + 1; });
    return counts;
  }, [filtered]);
  const selectedLinks = useMemo(() => {
    if (!filtered || !selected) return [];
    return filtered.edges.filter((edge) => edgeSource(edge) === selected.id || edgeTarget(edge) === selected.id);
  }, [filtered, selected]);

  const toggleType = (type: string) => setSelectedTypes((current) => {
    const next = new Set(current);
    if (next.has(type)) next.delete(type); else next.add(type);
    return next;
  });
  const toggleConfidence = (confidence: string) => setSelectedConfidence((current) => {
    const next = new Set(current);
    if (next.has(confidence)) next.delete(confidence); else next.add(confidence);
    return next;
  });
  const resetFilters = () => {
    setSelectedTypes(new Set(Object.keys(data.nodes_by_type)));
    setSelectedConfidence(new Set(CONFIDENCE_ORDER));
    setConnectivity("all");
    setFocusDirection("all");
    setQuery("");
  };

  return (
    <div className="graph-page">
      <div className="section-intro graph-intro">
        <div><span className="eyebrow">Source-derived topology</span><h2>Граф связей системы</h2><p>Граф хранится отдельным snapshot и доступен через MCP tool `kb_system_graph`. Точный rebuild временно заново клонирует удалённые checkout всех подключённых репозиториев, публикует полный граф и снова удаляет исходники; документы и RAG-индексы не меняются.</p></div>
        <div className="graph-head-actions">
          <div className="segmented"><button className={view === "services" ? "active" : ""} onClick={() => setView("services")}>Сервисы</button><button className={view === "full" ? "active" : ""} onClick={() => setView("full")}>Полный граф</button></div>
          <button className="button secondary" onClick={() => void onAction(() => post("/admin/api/graph/rebuild", password, { generation_mode: "static", verify_all: false }), "Быстрое перестроение отдельного графа запущено")}>⌘ Быстро</button>
          <button className="button precise" disabled={!gigacodeAvailable} title={!gigacodeAvailable ? gigacodeError || "GigaCode недоступен" : "Временно восстановить все удалённые checkout, проверить связи через GigaCode и снова удалить исходники"} onClick={() => void onAction(() => post("/admin/api/graph/rebuild", password, { generation_mode: "gigacode", verify_all: true }), "Точное перестроение отдельного графа запущено")}>✦ Точный rebuild</button>
        </div>
      </div>
      <div className="graph-toolbar">
        <label className="graph-search"><span>Поиск</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Сервис, API, событие…" /></label>
        <label><span>Связность</span><select value={connectivity} onChange={(event) => setConnectivity(event.target.value as ConnectivityFilter)}><option value="all">Все узлы</option><option value="connected">Есть любые связи</option><option value="incoming">Есть входящие</option><option value="outgoing">Есть исходящие</option><option value="bidirectional">Входящие и исходящие</option><option value="isolated">Изолированные</option></select></label>
        {selected && <label><span>Окружение выбранного</span><select value={focusDirection} onChange={(event) => setFocusDirection(event.target.value as FocusDirection)}><option value="all">Весь граф</option><option value="incoming">Только входящие</option><option value="outgoing">Только исходящие</option><option value="both">Оба направления</option></select></label>}
        <button className="button quiet" onClick={resetFilters}>Сбросить фильтры</button>
      </div>
      <div className="graph-layout">
        <aside className="graph-stats">
          <span className="eyebrow">Snapshot</span><h3>{number(filtered?.nodes.length || 0)} / {number(data.node_count)} узлов</h3>
          <div className="graph-metrics">
            <span><b>{number(filtered?.edges.length || 0)}</b> {view === "services" ? "видимых зависимостей" : "технических рёбер"}</span>
            {view === "services" ? <>
              <span><b>{number(data.resolved_service_dependency_count)}</b> сервис → сервис</span>
              <span><b>{number(data.external_dependency_count)}</b> во внешние системы</span>
              <span><b>{number(data.unresolved_dependency_count)}</b> не разрешено</span>
              <span><b>{number(data.isolated_service_count)}</b> изолированных сервисов</span>
              <span><b>{number(data.exitpoint_count)}</b> найденных выходов</span>
            </> : <>
              <span><b>{number(data.evidence_count)}</b> evidence</span>
              <span><b>{number(data.services.length)}</b> сервисов</span>
              <span><b>{number(data.issue_count)}</b> замечаний</span>
            </>}
          </div>
          <div className="snapshot-meta"><b>{data.analysis_mode === "static+gigacode" ? "✦ Static + GigaCode" : data.analysis_mode || "Static"}</b><code>{short(data.snapshot_id || "legacy snapshot", 28)}</code></div>
          <h4>Типы узлов · multi-select</h4>
          <div className="filter-chip-list">{Object.entries(data.nodes_by_type).map(([type, count]) => <button aria-pressed={selectedTypes.has(type)} className={selectedTypes.has(type) ? "active" : ""} key={type} onClick={() => toggleType(type)}><i style={{ background: nodeColor(type) }} /><span>{type}</span><b>{visibleTypeCounts[type] || 0}/{count}</b></button>)}</div>
          <h4>Уверенность связей</h4>
          <div className="confidence-list">{CONFIDENCE_ORDER.map((confidence) => <button aria-pressed={selectedConfidence.has(confidence)} className={selectedConfidence.has(confidence) ? "active" : ""} key={confidence} onClick={() => toggleConfidence(confidence)}><i style={{ background: confidenceColor(confidence) }} /><span>{CONFIDENCE_LABELS[confidence]}</span></button>)}</div>
        </aside>
        <section className="graph-canvas">{error ? <div className="empty-state"><h3>{error}</h3></div> : filtered && filtered.nodes.length ? <GraphCanvas graph={filtered} selected={selected?.id || null} onSelect={setSelected} /> : <div className="empty-state"><div>⌘</div><h3>По фильтрам ничего нет</h3><p>Сбросьте фильтры или запустите перестроение.</p></div>}</section>
          <aside className="graph-details">{selected ? <><span className="eyebrow">{selected.type}</span><h3>{selected.label}</h3><code>{selected.id}</code><div className="selected-edge-summary"><span><b>{selectedLinks.filter((edge) => edgeTarget(edge) === selected.id).length}</b> входящих</span><span><b>{selectedLinks.filter((edge) => edgeSource(edge) === selected.id).length}</b> исходящих</span></div><h4>Метаданные</h4><pre>{JSON.stringify(selected.metadata, null, 2)}</pre><h4>Evidence</h4><p>{selected.evidence_ids.length ? `${selected.evidence_ids.length} подтверждений в исходном коде` : "Для агрегированного узла evidence не записан."}</p><h4>Связи</h4><div className="edge-detail-list">{selectedLinks.slice(0, 40).map((edge) => <span key={edge.id}><i style={{ background: confidenceColor(edge.confidence) }} /><b>{edgeSource(edge) === selected.id ? "→" : "←"} {edge.type}</b><small>{Number(edge.metadata?.operation_count || 0) > 1 ? `${edge.metadata.operation_count} вызовов · ` : ""}{CONFIDENCE_LABELS[edge.confidence] || edge.confidence}</small></span>)}</div></> : <><div className="detail-placeholder">⌖</div><h3>Выберите узел</h3><p>Кликните узел: камера приблизится, здесь появятся направления связей, confidence и evidence.</p></>}</aside>
      </div>
    </div>
  );
}

function nodeColor(type: string): string {
  return ({ Service: "#b6f36b", ExternalSystem: "#ffb45d", BusinessOperation: "#78a7ff", BusinessRule: "#df83ff", EntryPoint: "#6ee7d8", ExitPoint: "#ff7e67", Event: "#ff7690", Table: "#f4d269", DomainEntity: "#a78bfa", Repository: "#8795aa" } as Record<string, string>)[type] || "#728096";
}

function confidenceColor(confidence: string): string {
  return ({ DECLARED: "#55e89a", HIGH: "#74a7ff", MEDIUM: "#f3c76b", LOW: "#ff9b55", UNRESOLVED: "#ff5f7d" } as Record<string, string>)[confidence] || "#8795aa";
}

function edgeSource(edge: GraphPayload["edges"][number]): string {
  return typeof edge.source === "object" ? edge.source.id : edge.source;
}

function edgeTarget(edge: GraphPayload["edges"][number]): string {
  return typeof edge.target === "object" ? edge.target.id : edge.target;
}

function canonicalizeGraph(graph: GraphPayload): GraphPayload {
  const nodes = [...new Map(graph.nodes.map((node) => [node.id, node])).values()];
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges = [...new Map(
    graph.edges
      .filter((edge) => nodeIds.has(edgeSource(edge)) && nodeIds.has(edgeTarget(edge)))
      .map((edge) => [edge.id, { ...edge, source: edgeSource(edge), target: edgeTarget(edge) }]),
  ).values()];
  return { ...graph, nodes, edges };
}

function filterGraph(
  graph: GraphPayload | null,
  selectedTypes: Set<string>,
  selectedConfidence: Set<string>,
  connectivity: ConnectivityFilter,
  selectedId: string | null,
  focusDirection: FocusDirection,
  query: string,
): GraphPayload | null {
  if (!graph) return null;
  const normalizedQuery = query.trim().toLowerCase();
  const baseNodes = graph.nodes.filter((node) => selectedTypes.has(node.type));
  const baseIds = new Set(baseNodes.map((node) => node.id));
  let edges = graph.edges.filter((edge) => selectedConfidence.has(edge.confidence) && baseIds.has(edgeSource(edge)) && baseIds.has(edgeTarget(edge)));
  const incoming = new Map<string, number>();
  const outgoing = new Map<string, number>();
  edges.forEach((edge) => {
    const source = edgeSource(edge), target = edgeTarget(edge);
    outgoing.set(source, (outgoing.get(source) || 0) + 1);
    incoming.set(target, (incoming.get(target) || 0) + 1);
  });
  let nodes = baseNodes.filter((node) => {
    const ins = incoming.get(node.id) || 0, outs = outgoing.get(node.id) || 0;
    if (connectivity === "connected") return ins + outs > 0;
    if (connectivity === "incoming") return ins > 0;
    if (connectivity === "outgoing") return outs > 0;
    if (connectivity === "bidirectional") return ins > 0 && outs > 0;
    if (connectivity === "isolated") return ins + outs === 0;
    return true;
  });
  if (normalizedQuery) {
    const matches = new Set(nodes.filter((node) => `${node.label} ${node.id} ${node.type} ${JSON.stringify(node.metadata)}`.toLowerCase().includes(normalizedQuery)).map((node) => node.id));
    edges.forEach((edge) => {
      if (matches.has(edgeSource(edge)) || matches.has(edgeTarget(edge))) {
        matches.add(edgeSource(edge)); matches.add(edgeTarget(edge));
      }
    });
    nodes = nodes.filter((node) => matches.has(node.id));
  }
  if (selectedId && focusDirection !== "all") {
    const neighbourhood = new Set([selectedId]);
    edges.forEach((edge) => {
      const source = edgeSource(edge), target = edgeTarget(edge);
      if (focusDirection === "outgoing" || focusDirection === "both") {
        if (source === selectedId) neighbourhood.add(target);
      }
      if (focusDirection === "incoming" || focusDirection === "both") {
        if (target === selectedId) neighbourhood.add(source);
      }
    });
    nodes = nodes.filter((node) => neighbourhood.has(node.id));
  }
  const visibleIds = new Set(nodes.map((node) => node.id));
  edges = edges.filter((edge) => visibleIds.has(edgeSource(edge)) && visibleIds.has(edgeTarget(edge)));
  return { ...graph, nodes, edges };
}

function GraphCanvas({ graph, selected, onSelect }: { graph: GraphPayload; selected: string | null; onSelect: (node: GraphNode) => void }) {
  return (
    <Suspense fallback={<div className="graph-loading"><span>✦</span><b>Загружаем 3D-движок…</b></div>}>
      <GraphCanvas3D graph={graph} selected={selected} onSelect={onSelect} />
    </Suspense>
  );
}
