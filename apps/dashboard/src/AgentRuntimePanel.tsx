import { useCallback, useEffect, useRef, useState } from "react";
import { accessError } from "./accessApi";
import { ApiError } from "./api";
import { integrationDate } from "./managementApi";

interface RuntimeSnapshot {
  enabled: boolean;
  available: boolean;
  state: "disabled" | "starting" | "recovering" | "authentication_required" | "ready" | "error";
  authentication_url: string | null;
  message: string;
  events: Array<{ timestamp: string; state: string; message: string }>;
}

type ManagementRequest = <T>(path: string, init?: RequestInit) => Promise<T>;
const labels: Record<RuntimeSnapshot["state"], string> = {
  disabled: "Агент выключен",
  starting: "Запускаем GigaCode",
  recovering: "GigaCode перезапускается",
  authentication_required: "Нужна авторизация GigaCode",
  ready: "Агент готов к работе",
  error: "Не удалось подготовить агента",
};
const pageVisible = () => document.visibilityState !== "hidden";

function authorizationUrl(snapshot: RuntimeSnapshot | null): string | null {
  if (snapshot?.state !== "authentication_required" || !snapshot.authentication_url) return null;
  try {
    const url = new URL(snapshot.authentication_url);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password
      ? url.href : null;
  } catch { return null; }
}

export default function AgentRuntimePanel({ request, revision }: {
  request: ManagementRequest; revision: number;
}) {
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const mounted = useRef(true);

  const refresh = useCallback(async (signal: AbortSignal) => {
    const current = generation.current;
    try {
      const result = await request<RuntimeSnapshot>("/admin/api/a2a/runtime", { signal });
      if (!signal.aborted && mounted.current && current === generation.current && document.visibilityState !== "hidden") {
        setSnapshot(result); setError("");
      }
    } catch (caught) {
      if (signal.aborted || !mounted.current || current !== generation.current) return;
      if (caught instanceof ApiError && [401, 403].includes(caught.status)) setSnapshot(null);
      else setSnapshot(value => value ? { ...value, authentication_url: null } : null);
      setError(accessError(caught));
    }
  }, [request]);

  useEffect(() => {
    mounted.current = true;
    let timer = 0;
    let controller: AbortController | null = null;
    const poll = async () => {
      if (!mounted.current || !pageVisible()) return;
      controller?.abort(); const active = new AbortController(); controller = active;
      await refresh(active.signal);
      if (mounted.current && controller === active && !active.signal.aborted && pageVisible()) {
        timer = window.setTimeout(() => void poll(), 2000);
      }
    };
    const visibility = () => {
      ++generation.current; controller?.abort(); window.clearTimeout(timer);
      setSnapshot(value => value ? { ...value, authentication_url: null } : null);
      if (document.visibilityState !== "hidden") void poll();
    };
    const hide = () => { ++generation.current; controller?.abort(); window.clearTimeout(timer); setSnapshot(value => value ? { ...value, authentication_url: null } : null); };
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", hide);
    void poll();
    return () => {
      mounted.current = false; ++generation.current; controller?.abort(); window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visibility); window.removeEventListener("pagehide", hide);
    };
  }, [refresh, revision]);

  const change = async (enable: boolean) => {
    if (busy) return;
    const current = ++generation.current;
    setBusy(true); setError("");
    setSnapshot(value => value ? { ...value, authentication_url: null } : null);
    try {
      const result = await request<RuntimeSnapshot>(`/admin/api/a2a/runtime/${enable ? "enable" : "disable"}`, { method: "POST", body: "{}" });
      if (mounted.current && current === generation.current && document.visibilityState !== "hidden") { ++generation.current; setSnapshot(result); }
    } catch (caught) {
      if (mounted.current && current === generation.current) {
        ++generation.current;
        if (caught instanceof ApiError && [401, 403].includes(caught.status)) setSnapshot(null);
        setError(accessError(caught));
      }
    } finally { if (mounted.current) setBusy(false); }
  };
  const authUrl = authorizationUrl(snapshot);
  return <section className="panel integration-panel">
    <div className="integration-job-head"><h3>Агент исследования · GigaCode</h3><span className="tag" role="status">{snapshot ? labels[snapshot.state] : "Проверяем состояние…"}</span></div>
    <p className="integration-muted">Включите агента один раз и пройдите авторизацию, если GigaCode её запросит. Он будет изучать доступные документы и собирать ответы на вопросы подключённых агентов.</p>
    {snapshot?.message && <p role="status">{snapshot.message}</p>}
    {error && <div className="form-error" role="alert">{error}</div>}
    <div className="integration-actions">
      <button className="button primary" disabled={busy || !snapshot || (snapshot.enabled && snapshot.state !== "error")} onClick={() => void change(true)}>{busy ? "Подождите…" : snapshot?.state === "error" ? "Повторить запуск агента" : "Включить агента"}</button>
      <button className="button secondary" disabled={busy || !snapshot?.enabled} onClick={() => void change(false)}>Отключить</button>
      {authUrl && <a className="button primary" href={authUrl} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">Пройти авторизацию GigaCode ↗</a>}
    </div>
    {snapshot?.state === "authentication_required" && <p className="integration-muted">{authUrl ? "Откройте ссылку, завершите вход и вернитесь сюда. Состояние обновится автоматически." : "Ожидаем ссылку авторизации от GigaCode…"}</p>}
    {Boolean(snapshot?.events?.length) && <details><summary>Последние шаги запуска</summary><pre className="integration-log">{snapshot?.events.slice(-12).map(event => `${integrationDate(event.timestamp)} · ${event.message}`).join("\n")}</pre></details>}
  </section>;
}
