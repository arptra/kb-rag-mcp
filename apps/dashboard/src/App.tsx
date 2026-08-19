import {
  FormEvent,
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { ApiError, api, post } from "./api";
import type {
  CatalogJob,
  GraphNode,
  GraphOverview,
  GraphPayload,
  ManagedTool,
  Overview,
  Page,
  RagIndex,
} from "./types";

const NAV: Array<{ id: Page; label: string; mark: string }> = [
  { id: "overview", label: "Обзор", mark: "◫" },
  { id: "indexes", label: "Индексы", mark: "◇" },
  { id: "tools", label: "MCP tools", mark: "⌁" },
  { id: "graph", label: "Граф системы", mark: "⌘" },
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

function number(value: number): string {
  return new Intl.NumberFormat("ru-RU").format(value ?? 0);
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

function Status({ value }: { value: string }) {
  return (
    <span className={`status status-${value}`}>
      <span /> {value === "ready" || value === "completed" ? "готов" : value}
    </span>
  );
}

function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="modal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <div>
            <span className="eyebrow">RAG control plane</span>
            <h2>{title}</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Закрыть">×</button>
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

export default function App() {
  const [password, setPassword] = useState(() => sessionStorage.getItem("rag-admin-password") || "");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [page, setPage] = useState<Page>("overview");
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [loading, setLoading] = useState(false);
  const [indexModal, setIndexModal] = useState(false);
  const [repositoryModal, setRepositoryModal] = useState(false);
  const [toolModal, setToolModal] = useState<ManagedTool | "new" | null>(null);

  const load = useCallback(async () => {
    if (!password) return;
    try {
      const payload = await api<Overview>("/admin/api/overview", password);
      setOverview(payload);
      setError("");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) {
        sessionStorage.removeItem("rag-admin-password");
        setOverview(null);
        setError(caught.message);
      } else {
        setError(caught instanceof Error ? caught.message : "Не удалось загрузить панель");
      }
    }
  }, [password]);

  useEffect(() => void load(), [load]);
  useEffect(() => {
    if (!overview?.catalog.jobs.some((job) => ["queued", "running"].includes(job.status))) return;
    const timer = window.setInterval(load, 2200);
    return () => window.clearInterval(timer);
  }, [overview, load]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 3600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const submitPassword = (value: string) => {
    sessionStorage.setItem("rag-admin-password", value);
    setPassword(value);
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

  if (!overview) return <Login error={error} onSubmit={submitPassword} />;

  const title = NAV.find((item) => item.id === page)?.label ?? "RAG Control Plane";
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">R</span><div><b>RAG</b><small>CONTROL PLANE</small></div></div>
        <nav>
          <span className="nav-label">Рабочая область</span>
          {NAV.map((item) => (
            <button key={item.id} className={page === item.id ? "active" : ""} onClick={() => setPage(item.id)}>
              <span className="nav-mark">{item.mark}</span>{item.label}
              {item.id === "tools" && <em>{overview.managed_tools.tool_count}</em>}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="server-state"><span className="pulse" /><div><b>MCP online</b><small>{overview.index.embedding_provider} embeddings</small></div></div>
          <button
            className="logout"
            onClick={() => {
              sessionStorage.removeItem("rag-admin-password");
              setPassword("");
              setOverview(null);
            }}
          >Сменить доступ</button>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div><span className="breadcrumb">RAG Control Plane /</span><h1>{title}</h1></div>
          <div className="top-actions">
            <button className="button quiet" onClick={() => void load()} disabled={loading}>↻ Обновить</button>
            {page === "indexes" && <button className="button primary" onClick={() => setRepositoryModal(true)}>＋ Подключить репозиторий</button>}
            {page === "tools" && <button className="button primary" onClick={() => setToolModal("new")}>＋ Новый MCP tool</button>}
          </div>
        </header>

        <section className="content">
          {page === "overview" && <OverviewPage data={overview} onNavigate={setPage} />}
          {page === "indexes" && (
            <IndexesPage
              data={overview}
              password={password}
              onCreate={() => setIndexModal(true)}
              onRepository={() => setRepositoryModal(true)}
              onAction={action}
            />
          )}
          {page === "tools" && (
            <ToolsPage
              data={overview}
              password={password}
              onEdit={setToolModal}
              onAction={action}
            />
          )}
          {page === "graph" && <GraphPage data={overview.graph} password={password} onAction={action} />}
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
          onClose={() => setRepositoryModal(false)}
          onSaved={() => {
            setRepositoryModal(false);
            setToast("Импорт поставлен в очередь");
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
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function OverviewPage({ data, onNavigate }: { data: Overview; onNavigate: (page: Page) => void }) {
  const activeJobs = data.catalog.jobs.filter((job) => ["queued", "running"].includes(job.status));
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
        <Metric label="MCP tools" value={number(data.managed_tools.tool_count + 7)} note={`${data.managed_tools.tool_count} управляемых`} tone="violet" />
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
          <JobList jobs={data.catalog.jobs.slice(0, 6)} />
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

function JobList({ jobs }: { jobs: CatalogJob[] }) {
  if (!jobs.length) return <div className="empty-state compact">Операций пока не было</div>;
  return <div className="job-list">{jobs.map((job) => (
    <div className="job-row" key={job.id}>
      <span className={`job-icon ${job.type}`}>{job.type === "repository" ? "↗" : job.type === "graph" ? "⌘" : "◇"}</span>
      <div className="grow"><b>{job.message}</b><small>{job.error || `${job.type} · ${relativeDate(job.completed_at || job.started_at)}`}</small></div>
      <Status value={job.status} />
    </div>
  ))}</div>;
}

function IndexesPage({ data, password, onCreate, onRepository, onAction }: {
  data: Overview;
  password: string;
  onCreate: () => void;
  onRepository: () => void;
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
          <article className="index-card" key={index.id}>
            <header><div className="index-glyph big">{index.kind === "default" ? "◆" : "◇"}</div><Status value={index.status} /></header>
            <h3>{index.name}</h3><p>{index.description || "Отдельный контур корпоративных знаний"}</p>
            <div className="index-numbers"><span><b>{number(index.document_count)}</b> документов</span><span><b>{number(index.chunk_count)}</b> чанков</span><span><b>{index.source_count}</b> Git</span></div>
            {index.error && <div className="inline-error">{index.error}</div>}
            <footer><small>Обновлён {relativeDate(index.updated_at)}</small><button onClick={() => void onAction(() => post("/admin/api/indexes/build", password, { index_id: index.id }), "Переиндексация запущена")}>↻ Пересобрать</button></footer>
          </article>
        ))}
      </div>
      <section className="panel repositories-panel">
        <PanelHeader title="Подключённые репозитории" kicker="Git → OpenSpec → RAG" action="Подключить" onAction={onRepository} />
        {data.catalog.repositories.length ? (
          <div className="table-wrap"><table><thead><tr><th>Репозиторий</th><th>Ветка / commit</th><th>Индекс</th><th>OpenSpec</th><th>Синхронизация</th></tr></thead><tbody>
            {data.catalog.repositories.map((repo) => <tr key={repo.id}><td><b>{repo.name}</b><small title={repo.git_url}>{short(repo.git_url, 56)}</small></td><td><code>{repo.ref || "HEAD"}</code><small>{repo.commit?.slice(0, 9) || "—"}</small></td><td><span className="tag">{nameById[repo.index_id] || repo.index_id}</span></td><td><b>{repo.document_count}</b><small>документов</small></td><td>{relativeDate(repo.synced_at)}</td></tr>)}
          </tbody></table></div>
        ) : <div className="empty-state"><div>↗</div><h3>Подключите первый репозиторий</h3><p>Сервис найдёт каталог openspec, обновит индекс и построит граф.</p><button className="button primary" onClick={onRepository}>Подключить Git</button></div>}
      </section>
      {data.catalog.jobs.length > 0 && <section className="panel"><PanelHeader title="Очередь операций" kicker="Фоновые задачи" /><JobList jobs={data.catalog.jobs} /></section>}
    </>
  );
}

function ToolsPage({ data, password, onEdit, onAction }: {
  data: Overview;
  password: string;
  onEdit: (tool: ManagedTool) => void;
  onAction: (run: () => Promise<unknown>, message: string) => Promise<void>;
}) {
  const [testing, setTesting] = useState<string | null>(null);
  const [query, setQuery] = useState("Как устроено текущее состояние системы?");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const indexNames = Object.fromEntries(data.catalog.indexes.map((index) => [index.id, index.name]));
  return (
    <>
      <div className="section-intro"><div><span className="eyebrow">Agent interfaces</span><h2>Управляемые MCP tools</h2><p>Каждый tool — безопасный поисковый контракт с собственным описанием, фильтрами и набором индексов.</p></div></div>
      <div className="tool-grid">
        {data.managed_tools.tools.map((tool) => (
          <article className="tool-card" key={tool.name}>
            <header><span className="tool-mark">⌁</span><Status value={tool.index_ids.length ? "ready" : "empty"} /><button className="dots" onClick={() => onEdit(tool)}>•••</button></header>
            <code>{tool.name}</code><p>{tool.description}</p>
            <div className="bindings">{tool.index_ids.length ? tool.index_ids.map((id) => <span className="tag" key={id}>◇ {indexNames[id] || id}</span>) : <span className="tag warning">Не привязан</span>}</div>
            <footer><span>top {tool.defaults.top_k} · {tool.defaults.status || "любой статус"}</span><button onClick={() => { setTesting(testing === tool.name ? null : tool.name); setResult(null); }}>Проверить →</button></footer>
            {testing === tool.name && <div className="tool-test"><input value={query} onChange={(event) => setQuery(event.target.value)} /><button className="button primary" onClick={() => void onAction(async () => { const payload = await post<Record<string, unknown>>("/admin/api/tools/test", password, { name: tool.name, query }); setResult(payload); }, "Тест завершён")}>Запустить</button>{result && <pre>{JSON.stringify(result, null, 2)}</pre>}</div>}
          </article>
        ))}
        {!data.managed_tools.tools.length && <button className="new-tool-card" onClick={() => document.querySelector<HTMLButtonElement>(".top-actions .primary")?.click()}><span>＋</span><b>Создать первый MCP tool</b><small>Выберите индексы и опишите агенту назначение поиска</small></button>}
      </div>
      {data.managed_tools.tools.length > 0 && <div className="danger-note"><span>i</span><p>Удаление tool сразу убирает его из MCP discovery. Клиентскому stdio-прокси потребуется перезапуск.</p><button onClick={() => { const name = window.prompt("Имя tool для удаления"); if (name) void onAction(() => post("/admin/api/tools/delete", password, { name }), "Tool удалён"); }}>Удалить tool</button></div>}
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

function RepositoryForm({ password, indexes, onClose, onSaved }: { password: string; indexes: RagIndex[]; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState("");
  const [gitUrl, setGitUrl] = useState("");
  const [ref, setRef] = useState("");
  const [target, setTarget] = useState(indexes[0]?.id || "__new__");
  const [indexName, setIndexName] = useState("");
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await post("/admin/api/repositories", password, { name, git_url: gitUrl, ref: ref || null, index_id: target === "__new__" ? null : target, index_name: target === "__new__" ? indexName || name : null });
      onSaved();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Не удалось подключить репозиторий"); }
  };
  return <Modal title="Подключить Git-репозиторий" onClose={onClose}><form className="modal-form" onSubmit={submit}><div className="field-row"><label>Название<input required minLength={2} value={name} onChange={(event) => setName(event.target.value)} placeholder="payments-service" /></label><label>Ref, необязательно<input value={ref} onChange={(event) => setRef(event.target.value)} placeholder="main / tag / commit" /></label></div><label>Git URL<input required value={gitUrl} onChange={(event) => setGitUrl(event.target.value)} placeholder="https://git.company.local/team/service.git" /></label><label>Целевой индекс<select value={target} onChange={(event) => setTarget(event.target.value)}>{indexes.map((index) => <option key={index.id} value={index.id}>{index.name}</option>)}<option value="__new__">＋ Создать новый индекс</option></select></label>{target === "__new__" && <label>Название нового индекса<input value={indexName} onChange={(event) => setIndexName(event.target.value)} placeholder={name || "System knowledge"} /></label>}<div className="flow-preview"><span>Git checkout</span><i>→</i><span>openspec/**</span><i>→</i><span>RAG index</span><i>＋</i><span>System graph</span></div>{error && <div className="form-error">{error}</div>}<div className="modal-actions"><button type="button" className="button quiet" onClick={onClose}>Отмена</button><button className="button primary">Подключить и индексировать</button></div></form></Modal>;
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

function GraphPage({ data, password, onAction }: { data: GraphOverview; password: string; onAction: (run: () => Promise<unknown>, message: string) => Promise<void> }) {
  const [view, setView] = useState<"services" | "full">("services");
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api<GraphPayload>(`/admin/api/graph?view=${view}&limit=${view === "services" ? 300 : 700}`, password).then((payload) => { setGraph(payload); setSelected(null); setError(""); }).catch((caught) => setError(caught instanceof Error ? caught.message : "Граф недоступен"));
  }, [password, view, data.generated_at]);
  return (
    <div className="graph-page">
      <div className="section-intro graph-intro"><div><span className="eyebrow">Source-derived topology</span><h2>Граф связей системы</h2><p>Первая версия извлекает сервисы, HTTP-вызовы, события, таблицы и business rules из подключённых репозиториев.</p></div><div className="segmented"><button className={view === "services" ? "active" : ""} onClick={() => setView("services")}>Сервисы</button><button className={view === "full" ? "active" : ""} onClick={() => setView("full")}>Полный граф</button></div><button className="button secondary" onClick={() => void onAction(() => post("/admin/api/graph/rebuild", password, {}), "Анализ графа запущен")}>⌘ Перестроить</button></div>
      <div className="graph-layout">
        <aside className="graph-stats"><span className="eyebrow">Snapshot</span><h3>{number(data.node_count)} узлов</h3><div className="graph-metrics"><span><b>{number(data.edge_count)}</b> связей</span><span><b>{number(data.evidence_count)}</b> evidence</span><span><b>{number(data.services.length)}</b> сервисов</span><span><b>{number(data.issue_count)}</b> замечаний</span></div><h4>Типы узлов</h4>{Object.entries(data.nodes_by_type).slice(0, 12).map(([type, count]) => <div className="legend-row" key={type}><i style={{ background: nodeColor(type) }} /><span>{type}</span><b>{count}</b></div>)}</aside>
        <section className="graph-canvas">{error ? <div className="empty-state"><h3>{error}</h3></div> : graph && graph.nodes.length ? <GraphCanvas graph={graph} selected={selected?.id || null} onSelect={setSelected} /> : <div className="empty-state"><div>⌘</div><h3>Граф пока пуст</h3><p>Подключите Git-репозитории или запустите перестроение.</p></div>}</section>
        <aside className="graph-details">{selected ? <><span className="eyebrow">{selected.type}</span><h3>{selected.label}</h3><code>{selected.id}</code><h4>Метаданные</h4><pre>{JSON.stringify(selected.metadata, null, 2)}</pre><h4>Evidence</h4><p>{selected.evidence_ids.length ? `${selected.evidence_ids.length} подтверждений в исходном коде` : "Для агрегированного узла evidence не записан."}</p></> : <><div className="detail-placeholder">⌖</div><h3>Выберите узел</h3><p>Здесь появятся тип, метаданные и ссылки на подтверждения в исходниках.</p></>}</aside>
      </div>
    </div>
  );
}

function nodeColor(type: string): string {
  return ({ Service: "#b6f36b", ExternalSystem: "#ffb45d", BusinessOperation: "#78a7ff", BusinessRule: "#df83ff", EntryPoint: "#6ee7d8", ExitPoint: "#ff7e67", Event: "#ff7690", Table: "#f4d269", DomainEntity: "#a78bfa", Repository: "#8795aa" } as Record<string, string>)[type] || "#728096";
}

function GraphCanvas({ graph, selected, onSelect }: { graph: GraphPayload; selected: string | null; onSelect: (node: GraphNode) => void }) {
  const positions = useMemo(() => {
    const width = 1100, height = 650, result: Record<string, { x: number; y: number }> = {};
    if (graph.view === "services") {
      graph.nodes.forEach((node, index) => { const angle = (index / Math.max(1, graph.nodes.length)) * Math.PI * 2 - Math.PI / 2; const radius = Math.min(250, 105 + graph.nodes.length * 14); result[node.id] = { x: width / 2 + Math.cos(angle) * radius, y: height / 2 + Math.sin(angle) * radius }; });
    } else {
      const columns = Math.max(4, Math.ceil(Math.sqrt(graph.nodes.length * 1.6)));
      graph.nodes.forEach((node, index) => { result[node.id] = { x: 55 + (index % columns) * (990 / Math.max(1, columns - 1)), y: 55 + Math.floor(index / columns) * 84 }; });
    }
    return result;
  }, [graph]);
  return <svg viewBox="0 0 1100 650" role="img" aria-label="Граф связей системы"><defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>{graph.edges.map((edge) => { const from = positions[edge.source], to = positions[edge.target]; if (!from || !to) return null; return <line key={edge.id} className={edge.confidence === "LOW" || edge.confidence === "UNRESOLVED" ? "uncertain" : ""} x1={from.x} y1={from.y} x2={to.x} y2={to.y} markerEnd="url(#arrow)" />; })}{graph.nodes.map((node) => { const position = positions[node.id]; const radius = node.type === "Service" ? 24 : 15; return <g key={node.id} className={`graph-node ${selected === node.id ? "selected" : ""}`} transform={`translate(${position.x} ${position.y})`} onClick={() => onSelect(node)}><circle r={radius} fill={nodeColor(node.type)} /><text y={radius + 18}>{short(node.label, 22)}</text><title>{node.type}: {node.label}</title></g>; })}</svg>;
}
