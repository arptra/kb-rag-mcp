import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { accessApi, accessError, accessPost } from "./accessApi";
import type { AccessAdminSession } from "./accessTypes";

/** Admin secrets and the CSRF value stay in memory; these requests never reset a reader session. */
export function useManagementApi(initialPassword = "") {
  const [password, setPassword] = useState(initialPassword);
  const [revision, setRevision] = useState(0);
  const csrf = useRef("");
  useEffect(() => { setPassword(initialPassword); }, [initialPassword]);
  useEffect(() => {
    const controller = new AbortController();
    void accessApi<AccessAdminSession>("/access/api/session", { signal: controller.signal })
      .then(session => { csrf.current = session.csrf_token; setRevision(value => value + 1); })
      .catch(() => { /* A reader or a legacy deployment need not have an admin cookie. */ });
    return () => controller.abort();
  }, []);
  const request = useCallback(<T,>(path: string, init: RequestInit = {}): Promise<T> => {
    const headers = new Headers(init.headers);
    if (password) headers.set("X-KB-Admin-Password", password);
    if (csrf.current && init.method && init.method !== "GET") headers.set("X-CSRF-Token", csrf.current);
    return accessApi<T>(path, { ...init, headers });
  }, [password]);
  const login = async (username: string, secret: string, legacy: boolean) => {
    if (legacy) setPassword(secret);
    else {
      const session = await accessPost<AccessAdminSession>("/access/api/login", { username, password: secret });
      csrf.current = session.csrf_token;
    }
    setRevision(value => value + 1);
  };
  return { request, login, revision };
}

export function ManagementLogin({ onLogin }: { onLogin: (username: string, password: string, legacy: boolean) => Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [legacy, setLegacy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try { await onLogin(username.trim(), password, legacy); setPassword(""); }
    catch (caught) { setError(accessError(caught)); }
    finally { setBusy(false); }
  };
  return <section className="panel integration-panel"><h3>Вход администратора</h3><p className="integration-muted">Для настройки источников и выдачи A2A-токенов нужен отдельный доступ администратора.</p>
    <form className="modal-form" onSubmit={event => void submit(event)}>
      <label>Способ входа<select value={legacy ? "legacy" : "account"} onChange={event => setLegacy(event.target.value === "legacy")}><option value="account">Учётная запись администратора</option><option value="legacy">Пароль администратора сервера</option></select></label>
      {!legacy && <label>Логин<input required value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" /></label>}
      <label>Пароль<input required type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" /></label>
      {error && <div className="form-error" role="alert">{error}</div>}
      <div><button className="button primary" disabled={busy}>{busy ? "Входим…" : "Войти"}</button></div>
    </form>
  </section>;
}

export function integrationDate(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString("ru-RU");
}
