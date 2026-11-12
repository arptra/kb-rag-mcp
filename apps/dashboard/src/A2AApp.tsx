import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { accessError } from "./accessApi";
import { ApiError } from "./api";
import { ManagementLogin, integrationDate, useManagementApi } from "./managementApi";
import type { RagIndex } from "./types";
import "./integrations.css";

interface A2AAgent {
  id: string; name: string; index_ids: string[]; status?: string; created_at?: string;
  expires_at?: string | number; last_used_at?: string; token_prefix?: string; prefix?: string;
  call_count?: number; revoked_at?: string | null;
}
interface A2ACall {
  id: string; created_at?: string; timestamp?: string; agent_id: string; agent_name?: string;
  method?: string; status?: string; duration_ms?: number; error?: string | null;
  action?: string; index_id?: string; version_scope?: string; query?: string;
}
interface A2AStatus { enabled?: boolean; can_manage: boolean; endpoint: string; agent_card_url: string; protocol_version: string }
interface A2ACatalog extends A2AStatus {
  agents: A2AAgent[]; calls: A2ACall[]; indexes?: Array<Pick<RagIndex, "id" | "name">>;
  stats: { total_calls?: number; successful_calls?: number; failed_calls?: number; active_agents?: number };
}
interface A2AIssued {
  agent: A2AAgent; token: string; endpoint: string; agent_card_url: string;
  expires_at: string | number; config: Record<string, unknown>;
}

export default function A2AApp({ password = "", indexes = [], connectionMode = false }: {
  password?: string; indexes?: Array<Pick<RagIndex, "id" | "name">>; connectionMode?: boolean;
}) {
  const { request, login, revision } = useManagementApi(password);
  const [status, setStatus] = useState<A2AStatus | null>(null);
  const [catalog, setCatalog] = useState<A2ACatalog | null>(null);
  const [calls, setCalls] = useState<A2ACall[]>([]);
  const [cursor, setCursor] = useState<string | number | null>(null);
  const callCursor = useRef<string | number | null>(null);
  const [agentFilter, setAgentFilter] = useState("");
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [days, setDays] = useState(30);
  const [issued, setIssued] = useState<A2AIssued | null>(null);
  const [tab, setTab] = useState<"agents" | "calls">("agents");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const secretField = useRef<HTMLTextAreaElement>(null);
  const generation = useRef(0);
  const choices = indexes.length ? indexes : catalog?.indexes ?? [];

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      const current = await request<A2AStatus>("/auth/a2a/status", { signal });
      setStatus(current);
      if (current.enabled === false) { ++generation.current; setCatalog(null); setIssued(null); setLoadError(""); return; }
      try {
        const data = await request<A2ACatalog>("/admin/api/a2a", { signal });
        setCatalog(data); setStatus(data); setLoadError("");
      } catch (caught) {
        if (caught instanceof ApiError && [401, 403].includes(caught.status) && !current.can_manage) { setCatalog(null); setIssued(null); setLoadError(""); }
        else throw caught;
      }
    } catch (caught) { if (!signal?.aborted) setLoadError(accessError(caught)); }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [request, password]);
  useEffect(() => {
    const controller = new AbortController(); let timer = 0;
    const poll = async () => { await load(controller.signal); if (!controller.signal.aborted) timer = window.setTimeout(poll, 5000); };
    void poll(); return () => { controller.abort(); window.clearTimeout(timer); };
  }, [load, revision]);
  useEffect(() => {
    const hide = () => { ++generation.current; setIssued(null); };
    const visibility = () => { if (document.visibilityState === "hidden") hide(); };
    document.addEventListener("visibilitychange", visibility); window.addEventListener("pagehide", hide);
    return () => { hide(); document.removeEventListener("visibilitychange", visibility); window.removeEventListener("pagehide", hide); };
  }, []);
  useEffect(() => {
    if (!issued) return;
    const expiry = typeof issued.expires_at === "number" ? issued.expires_at * 1000 : new Date(issued.expires_at).getTime();
    if (!Number.isFinite(expiry)) return;
    const timer = window.setTimeout(() => setIssued(null), Math.max(0, Math.min(expiry - Date.now(), 2_147_483_647)));
    return () => window.clearTimeout(timer);
  }, [issued]);

  const loadCalls = useCallback(async (next: string | number | null = null, signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: "50" });
    if (next !== null) params.set("cursor", String(next));
    if (agentFilter) params.set("agent_id", agentFilter);
    try {
      const data = await request<{ calls: A2ACall[]; next_cursor: string | number | null }>(`/admin/api/a2a/calls?${params}`, { signal });
      if (signal?.aborted) return;
      setCalls(current => next !== null ? [...new Map([...current, ...data.calls].map(call => [call.id, call])).values()].slice(-500) : data.calls);
      callCursor.current = data.next_cursor; setCursor(data.next_cursor); setError("");
    } catch (caught) { if (!signal?.aborted) setError(accessError(caught)); }
  }, [request, agentFilter]);
  useEffect(() => {
    if (!catalog || tab !== "calls") return;
    const controller = new AbortController(); let timer = 0; callCursor.current = null; setCalls([]);
    const poll = async () => { await loadCalls(callCursor.current, controller.signal); if (!controller.signal.aborted) timer = window.setTimeout(poll, 5000); };
    void poll(); return () => { controller.abort(); window.clearTimeout(timer); };
  }, [Boolean(catalog), tab, loadCalls]);

  const issue = async (event?: FormEvent, agent?: A2AAgent) => {
    event?.preventDefault(); if (busy) return;
    const current = ++generation.current; setIssued(null); setBusy(true); setError(""); setNotice("");
    try {
      const path = agent ? `/admin/api/a2a/agents/${encodeURIComponent(agent.id)}/rotate` : "/admin/api/a2a/agents";
      const result = await request<A2AIssued>(path, { method: "POST", body: JSON.stringify(agent ? {} : { name: name.trim(), index_ids: selected, expires_in_days: days }) });
      if (current === generation.current && document.visibilityState !== "hidden") setIssued(result);
      setName(""); await load();
    } catch (caught) { setError(accessError(caught)); }
    finally { setBusy(false); }
  };
  const revoke = async (agent: A2AAgent) => {
    if (!window.confirm(`Отозвать доступ A2A для «${agent.name}»? Его токен перестанет работать.`)) return;
    setBusy(true); setError("");
    try { await request(`/admin/api/a2a/agents/${encodeURIComponent(agent.id)}/revoke`, { method: "POST", body: "{}" }); setIssued(null); setNotice("Доступ A2A отозван"); await load(); }
    catch (caught) { setError(accessError(caught)); } finally { setBusy(false); }
  };
  const secretText = issued ? JSON.stringify(issued.config ?? { endpoint: issued.endpoint, agent_card_url: issued.agent_card_url, headers: { Authorization: `Bearer ${issued.token}`, "A2A-Version": "1.0" } }, null, 2) : "";
  const copy = async () => {
    try { await navigator.clipboard.writeText(secretText); setNotice("A2A-конфиг скопирован"); }
    catch { secretField.current?.focus(); secretField.current?.select(); setNotice("Конфиг выделен. Скопируйте его вручную."); }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([secretText], { type: "application/json" })); const link = document.createElement("a");
    link.href = url; link.download = "corporate-a2a.json"; document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <div className="integration-area">
    <div className="section-intro"><div><span className="eyebrow">Agent-to-agent</span><h2>{connectionMode ? "Подключить A2A" : "A2A · агенты и доступ"}</h2><p>Отдельные токены для агентов, доступ к выбранным индексам и собственный журнал вызовов.</p></div><button className="button secondary" disabled={loading} onClick={() => void load()}>Обновить</button></div>
    {status && <div className="integration-endpoints"><span><small>A2A endpoint</small><code>{status.endpoint || "—"}</code></span><span><small>Agent Card · версия {status.protocol_version || "—"}</small>{status.agent_card_url ? <a href={status.agent_card_url} target="_blank" rel="noreferrer">Открыть описание агента ↗</a> : "—"}</span></div>}
    {(error || loadError) && <div className="form-error" role="alert">{error || loadError}</div>}{notice && <div className="access-notice" role="status">{notice}</div>}
    {loading && !status && <div className="empty-state compact">Проверяем доступ A2A…</div>}
    {status?.enabled === false && <div className="server-note warning">A2A отключён на сервере.</div>}
    {status && status.enabled !== false && !catalog?.can_manage && <ManagementLogin onLogin={login} />}
    {catalog && status?.enabled !== false && <>
      <div className="server-summary integration-metrics"><span><b>{catalog.agents.length}</b> агентов</span><span><b>{catalog.stats.total_calls ?? 0}</b> вызовов A2A</span><span><b>{catalog.stats.failed_calls ?? 0}</b> ошибок</span></div>
      <div className="integration-tabs"><button className={`button ${tab === "agents" ? "primary" : "quiet"}`} onClick={() => setTab("agents")}>Агенты и токены</button><button className={`button ${tab === "calls" ? "primary" : "quiet"}`} onClick={() => setTab("calls")}>Журнал A2A</button></div>
      {tab === "agents" ? <>
        {catalog.can_manage && <section className="panel integration-panel"><h3>Выдать доступ агенту</h3><form className="modal-form" onSubmit={event => void issue(event)}>
          <div className="field-row"><label>Название агента<input required maxLength={120} value={name} onChange={event => setName(event.target.value)} placeholder="Помощник команды платформы" /></label><label>Срок, дней<input type="number" required min={1} max={365} value={days} onChange={event => setDays(Number(event.target.value))} /></label></div>
          <fieldset><legend>Доступные индексы</legend><div className="check-grid">{choices.map(index => <label className="check" key={index.id}><input type="checkbox" checked={selected.includes(index.id)} onChange={() => setSelected(current => current.includes(index.id) ? current.filter(id => id !== index.id) : [...current, index.id])} /><span>{index.name}</span></label>)}</div>{!choices.length && <small>Нет доступных индексов. Создайте индекс в дашборде.</small>}</fieldset>
          <div><button className="button primary" disabled={busy || !selected.length || !name.trim()}>{busy ? "Выдаём…" : "Создать агента и выдать токен"}</button></div>
        </form></section>}
        {issued && <section className="panel integration-panel" aria-live="polite"><h3>Токен выдан · {issued.agent.name}</h3><p className="integration-muted">Показывается один раз. Действует до {integrationDate(issued.expires_at)}. Сохраните конфиг перед уходом со страницы.</p><textarea ref={secretField} className="connect-json" value={secretText} readOnly autoComplete="off" spellCheck={false} aria-label="Конфиг A2A с секретным токеном" /><div className="integration-actions"><button className="button primary" onClick={() => void copy()}>Скопировать</button><button className="button secondary" onClick={download}>Скачать конфиг</button><button className="button quiet" onClick={() => setIssued(null)}>Скрыть</button></div></section>}
        <section className="panel"><div className="panel-header"><div><h3>Зарегистрированные агенты</h3></div></div><div className="table-wrap"><table><thead><tr><th>Агент</th><th>Индексы</th><th>Токен</th><th>Последний вызов</th><th /></tr></thead><tbody>{catalog.agents.map(agent => <tr key={agent.id}><td><b>{agent.name}</b><small>{agent.status || (agent.revoked_at ? "Отозван" : "Активен")}</small></td><td>{agent.index_ids.map(id => <span className="tag" key={id}>{choices.find(index => index.id === id)?.name || id}</span>)}</td><td><code>{agent.token_prefix || agent.prefix || "Скрыт"}</code><small>До {integrationDate(agent.expires_at)}</small></td><td>{integrationDate(agent.last_used_at)}</td><td>{catalog.can_manage && <div className="integration-actions"><button className="button quiet" disabled={busy || Boolean(agent.revoked_at) || agent.status === "revoked"} onClick={() => { if (window.confirm(`Заменить токен «${agent.name}»? Старый токен перестанет работать.`)) void issue(undefined, agent); }}>Заменить токен</button><button className="button quiet" disabled={busy || Boolean(agent.revoked_at) || agent.status === "revoked"} onClick={() => void revoke(agent)}>Отозвать</button></div>}</td></tr>)}</tbody></table>{!catalog.agents.length && <div className="empty-state compact">Агенты пока не зарегистрированы</div>}</div></section>
      </> : <section className="panel integration-panel"><div className="integration-actions"><label>Агент<select value={agentFilter} onChange={event => setAgentFilter(event.target.value)}><option value="">Все агенты</option>{catalog.agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label><button className="button secondary" onClick={() => void loadCalls(cursor)}>Проверить новые записи</button><small className="integration-muted">Обновляется каждые 5 секунд · последние 500 записей</small></div><div className="table-wrap"><table><thead><tr><th>Время</th><th>Агент</th><th>Операция</th><th>Результат</th><th>Время, мс</th></tr></thead><tbody>{calls.map(call => <tr key={call.id}><td>{integrationDate(call.created_at || call.timestamp)}</td><td>{call.agent_name || catalog.agents.find(agent => agent.id === call.agent_id)?.name || call.agent_id}</td><td>{call.method || "—"}{call.action && <small>{call.action}{call.index_id ? ` · ${call.index_id}` : ""}{call.version_scope ? ` · ${call.version_scope}` : ""}</small>}{call.query && <small>{call.query}</small>}</td><td>{call.status || "—"}{call.error && <small className="integration-error">{call.error}</small>}</td><td>{call.duration_ms ?? "—"}</td></tr>)}</tbody></table></div>{!calls.length && <div className="empty-state compact">Вызовов пока нет</div>}</section>}
    </>}
  </div>;
}
