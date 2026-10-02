import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { accessApi, accessError, accessPost } from "./accessApi";
import type { AccessAdmin, AccessAdminSession, AccessEvent, AccessPage, AccessTokenRecord, AccessUser } from "./accessTypes";

type Tab = "users" | "tokens" | "admins" | "events";
type Lists = {
  users: AccessPage<AccessUser> | null;
  tokens: AccessPage<AccessTokenRecord> | null;
  admins: { items: AccessAdmin[] } | null;
  events: AccessPage<AccessEvent> | null;
};
type Confirmation = { title: string; text: string; path: string; reason: boolean; self?: boolean; destructive: boolean };

const EMPTY_LISTS: Lists = { users: null, tokens: null, admins: null, events: null };
const PAGE_SIZE = 50;
const TABS: { id: Tab; title: string; description: string }[] = [
  { id: "users", title: "Пользователи", description: "Сертификаты, даты доступа и блокировка пользователя во всём сервисе." },
  { id: "tokens", title: "Токены и сессии", description: "Выданные учётные данные: сроки действия, последнее использование и отзыв." },
  { id: "admins", title: "Администраторы", description: "Отдельные учётные записи для управления доступом. Они не заменяют личный сертификат." },
  { id: "events", title: "Журнал аудита", description: "Кто, когда и какие изменения доступа выполнял." },
];

function date(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString("ru-RU");
}

function Status({ value }: { value: AccessUser["status"] | AccessTokenRecord["status"] | AccessAdmin["status"] }) {
  const labels = { active: "Активен", revoked: "Отозван", expired: "Истёк", disabled: "Отключён" };
  return <span className={`access-status ${value}`}>{labels[value]}</span>;
}

function Pagination({ offset, total, busy, onChange }: { offset: number; total: number; busy: boolean; onChange: (offset: number) => void }) {
  return (
    <footer className="access-pagination">
      <span>{total ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, total)} из ${total}` : "Нет записей"}</span>
      <div>
        <button className="button secondary" disabled={busy || offset === 0} onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}>← Назад</button>
        <button className="button secondary" disabled={busy || offset + PAGE_SIZE >= total} onClick={() => onChange(offset + PAGE_SIZE)}>Далее →</button>
      </div>
    </footer>
  );
}

function Login({ busy, error, onSubmit }: { busy: boolean; error: string; onSubmit: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    await onSubmit(username.trim(), password);
    setPassword("");
  };
  return (
    <main className="login-shell access-login">
      <section className="login-card">
        <div className="brand-mark large">R</div>
        <span className="eyebrow">Управление доступом</span>
        <h1>Вход администратора</h1>
        <p>Отдельный вход для управления пользователями, сертификатами и токенами.</p>
        <form onSubmit={(event) => void submit(event)}>
          <label>Логин<input autoFocus required autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} disabled={busy} maxLength={128} /></label>
          <label>Пароль<input required type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} maxLength={1024} /></label>
          {error && <div className="form-error" role="alert">{error}</div>}
          <button className="button primary wide" disabled={busy || !username.trim() || !password}>{busy ? "Выполняем вход…" : "Войти"}</button>
        </form>
        <small>Первый администратор задаётся при настройке сервера. Пароль не сохраняется в хранилище браузера.</small>
        <a className="access-link" href="/admin">← Вернуться к дашборду</a>
      </section>
    </main>
  );
}

export default function AccessAdminApp() {
  const [session, setSession] = useState<AccessAdminSession | null>(null);
  const [booting, setBooting] = useState(true);
  const [unavailable, setUnavailable] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [tab, setTab] = useState<Tab>("users");
  const [lists, setLists] = useState<Lists>(EMPTY_LISTS);
  const [offsets, setOffsets] = useState<Record<Tab, number>>({ users: 0, tokens: 0, admins: 0, events: 0 });
  const [userFilter, setUserFilter] = useState("");
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [reason, setReason] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const generation = useRef(0);
  const offset = offsets[tab];

  const clearSession = useCallback(() => {
    setSession(null);
    setLists(EMPTY_LISTS);
    setConfirmation(null);
    setNewPassword("");
  }, []);

  const restore = useCallback(async () => {
    const request = ++generation.current;
    setBooting(true);
    setUnavailable(false);
    setDisabled(false);
    setError("");
    try {
      const result = await accessApi<AccessAdminSession>("/access/api/session");
      if (request === generation.current) setSession(result);
    } catch (caught) {
      if (request !== generation.current) return;
      if (caught instanceof ApiError && caught.status === 401) clearSession();
      else if (caught instanceof ApiError && caught.status === 404) setDisabled(true);
      else {
        setUnavailable(true);
        setError(accessError(caught));
      }
    } finally {
      if (request === generation.current) setBooting(false);
    }
  }, [clearSession]);

  useEffect(() => {
    void restore();
    return () => { ++generation.current; };
  }, [restore]);

  useEffect(() => {
    if (!session) return;
    const controller = new AbortController();
    let current = true;
    setLoading(true);
    setLists((values) => ({ ...values, [tab]: null }));
    setError("");
    const load = async () => {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
      if (tab === "tokens" && userFilter) params.set("user_id", userFilter);
      try {
        const path = `/access/api/${tab}${tab === "admins" ? "" : `?${params}`}`;
        const result = await accessApi<AccessPage<AccessUser | AccessTokenRecord | AccessAdmin | AccessEvent>>(path, { signal: controller.signal });
        if (!current) return;
        if (tab !== "admins" && offset > 0 && offset >= result.total) {
          setOffsets((values) => ({ ...values, [tab]: Math.max(0, Math.floor((result.total - 1) / PAGE_SIZE) * PAGE_SIZE) }));
          return;
        }
        // The selected endpoint determines the corresponding record type.
        setLists((values) => ({ ...values, [tab]: result } as Lists));
      } catch (caught) {
        if (!current) return;
        if (caught instanceof ApiError && caught.status === 401) clearSession();
        setError(accessError(caught));
      } finally {
        if (current) setLoading(false);
      }
    };
    void load();
    return () => { current = false; controller.abort(); };
  }, [session, tab, offset, userFilter, revision, clearSession]);

  const login = async (username: string, password: string) => {
    setBusy(true);
    setError("");
    try {
      const result = await accessPost<AccessAdminSession>("/access/api/login", { username, password });
      setSession(result);
      setNotice("");
    } catch (caught) {
      setError(accessError(caught));
    } finally {
      setBusy(false);
    }
  };

  const mutate = async (path: string, body: unknown, success: string): Promise<boolean> => {
    if (!session || busy) return false;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await accessPost(path, body, session.csrf_token);
      setNotice(success);
      setRevision((value) => value + 1);
      return true;
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) clearSession();
      setError(accessError(caught));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    if (await mutate("/access/api/logout", {}, "")) clearSession();
  };

  const confirm = (value: Confirmation) => {
    setReason("");
    setConfirmation(value);
  };

  const applyConfirmation = async (event: FormEvent) => {
    event.preventDefault();
    if (!confirmation) return;
    const result = await mutate(confirmation.path, confirmation.reason ? { reason: reason.trim() } : {}, "Изменение доступа сохранено");
    if (result) {
      setConfirmation(null);
      if (confirmation.self) clearSession();
    }
  };

  const showTokens = (userId: string) => {
    setUserFilter(userId);
    setOffsets((values) => ({ ...values, tokens: 0 }));
    setTab("tokens");
  };

  const addAdmin = async (event: FormEvent) => {
    event.preventDefault();
    if (await mutate("/access/api/admins", { username: newUsername.trim(), password: newPassword }, "Администратор добавлен")) {
      setNewUsername("");
      setNewPassword("");
    }
  };

  if (booting || unavailable || disabled) {
    return <main className="login-shell access-login"><section className="login-card">
      <div className="brand-mark large">R</div><span className="eyebrow">Управление доступом</span>
      <h1>{booting ? "Проверяем сессию…" : disabled ? "Управление доступом отключено" : "Сервис недоступен"}</h1>
      {disabled && <p>Новый режим доступа не включён в конфигурации сервера. Обратитесь к ответственному за развёртывание.</p>}
      {error && <div className="form-error" role="alert">{error}</div>}
      {!booting && <button className="button secondary wide" onClick={() => void restore()}>Повторить проверку</button>}
      <a className="access-link" href="/admin">← Дашборд</a>
    </section></main>;
  }

  if (!session) return <Login busy={busy} error={error} onSubmit={login} />;

  const selected = TABS.find((item) => item.id === tab)!;
  const total = tab === "admins" ? lists.admins?.items.length ?? 0 : lists[tab]?.total ?? 0;
  const loaded = lists[tab] !== null;

  return (
    <div className="access-shell">
      <header className="access-header">
        <a className="access-brand" href="/admin"><span className="brand-mark">R</span><span><b>RAG</b><small>УПРАВЛЕНИЕ ДОСТУПОМ</small></span></a>
        <div className="access-header-actions"><span>Администратор: <b>{session.admin.username}</b></span><a className="button secondary" href="/admin">Дашборд ↗</a><button className="button secondary" onClick={() => void logout()} disabled={busy}>Выйти</button></div>
      </header>
      <main className="access-main">
        <div className="access-intro"><div><span className="eyebrow">Безопасность сервиса</span><h1>Управление доступом</h1><p>Доступ к инструментам и дашборду выдаётся по подтверждённому сертификату. Права администраторов управляются отдельно.</p></div><button className="button secondary" onClick={() => setRevision((value) => value + 1)} disabled={loading || busy}>↻ Обновить</button></div>
        <nav className="access-tabs" aria-label="Разделы управления доступом">{TABS.map((item) => <button key={item.id} className={tab === item.id ? "active" : ""} aria-current={tab === item.id ? "page" : undefined} onClick={() => { setTab(item.id); setNotice(""); }}>{item.title}</button>)}</nav>
        <section className="access-section" aria-labelledby="access-section-title">
          <div className="access-section-head"><div><h2 id="access-section-title">{selected.title}</h2><p>{selected.description}</p></div>{loaded && !loading && <span className="access-count">{total} записей</span>}</div>
          {error && <div className="form-error" role="alert">{error}</div>}
          {notice && <div className="access-notice" role="status">{notice}</div>}
          {tab === "tokens" && <div className="access-filter"><span>{userFilter ? <>Пользователь: <code>{userFilter}</code></> : "Все пользователи"}</span>{userFilter && <button className="button quiet" onClick={() => showTokens("")}>Сбросить фильтр ×</button>}</div>}
          {tab === "tokens" && <p className="access-help">Здесь отображаются только идентификаторы и префиксы, не секретные значения токенов. Отзыв токена отключает только соответствующую сессию; чтобы запретить повторную выдачу, отзовите доступ пользователя.</p>}
          {loading ? <p className="access-empty" role="status">Загружаем записи…</p> : loaded && total === 0 ? <p className="access-empty">{tab === "users" ? "Пользователей пока нет. Записи появятся после первого успешного входа по сертификату." : tab === "tokens" ? "Токенов и сессий для выбранного фильтра пока нет." : tab === "events" ? "В журнале пока нет событий." : "Администраторов не найдено."}</p> : !loaded ? <p className="access-empty">Записи не загружены. Нажмите «Обновить», чтобы повторить запрос.</p> : (
            <div className="access-table-wrap">
              {tab === "users" && <table className="access-table"><thead><tr><th>Пользователь / сертификат</th><th>Состояние</th><th>Даты доступа</th><th>Действия</th></tr></thead><tbody>{lists.users?.items.map((user) => <tr key={user.id}>
                <td className="access-identity"><b>{user.subject || user.id}</b><small>ID: {user.id}</small><details><summary>Сведения о сертификате</summary><dl><dt>Издатель</dt><dd>{user.issuer}</dd><dt>Серийный номер</dt><dd><code>{user.serial_number}</code></dd><dt>SHA-256 fingerprint</dt><dd><code>{user.fingerprint}</code></dd><dt>Действителен</dt><dd>{date(user.not_before)} — {date(user.not_after)}</dd></dl></details></td>
                <td><Status value={user.status} />{user.revoked_at && <small>Отозван: {date(user.revoked_at)}</small>}{user.revocation_reason && <small className="access-reason">{user.revocation_reason}</small>}</td>
                <td><small>Впервые</small><span>{date(user.created_at)}</span><small>Последний доступ</small><span>{date(user.last_seen_at)}</span></td>
                <td><div className="access-row-actions"><button className="button secondary" onClick={() => showTokens(user.id)}>Токены →</button>{user.status === "active" ? <button className="button access-danger" disabled={busy} onClick={() => confirm({ title: "Отозвать доступ пользователя?", text: `${user.subject || user.id}: все токены и браузерные сессии будут отозваны. Новый вход с этим сертификатом будет запрещён до восстановления доступа.`, path: `/access/api/users/${encodeURIComponent(user.id)}/revoke`, reason: true, destructive: true })}>Отозвать доступ</button> : <button className="button secondary" disabled={busy} onClick={() => confirm({ title: "Восстановить доступ?", text: `${user.subject || user.id} сможет снова получить токен по действительному сертификату. Старые отозванные токены не восстановятся.`, path: `/access/api/users/${encodeURIComponent(user.id)}/restore`, reason: false, destructive: false })}>Восстановить</button>}</div></td>
              </tr>)}</tbody></table>}
              {tab === "tokens" && <table className="access-table"><thead><tr><th>Токен / пользователь</th><th>Состояние</th><th>Выдан / истекает</th><th>Использование</th><th>Действия</th></tr></thead><tbody>{lists.tokens?.items.map((token) => <tr key={token.id}>
                <td><b><code>{token.prefix}…</code></b><small>ID: {token.id}</small><button className="access-inline-button" onClick={() => showTokens(token.user_id)}>{token.user_id}</button></td><td><Status value={token.status} /></td><td><small>Выдан</small><span>{date(token.created_at)}</span><small>Истекает</small><span>{date(token.expires_at)}</span></td><td><small>Последнее использование</small><span>{date(token.last_used_at)}</span>{token.revoked_at && <><small>Отозван</small><span>{date(token.revoked_at)}</span></>}</td><td><button className="button access-danger" disabled={busy || token.status !== "active"} onClick={() => confirm({ title: "Отозвать токен?", text: `Токен ${token.prefix}… перестанет давать доступ. Пользователь с активным сертификатом сможет получить новый.`, path: `/access/api/tokens/${encodeURIComponent(token.id)}/revoke`, reason: true, destructive: true })}>Отозвать</button></td>
              </tr>)}</tbody></table>}
              {tab === "admins" && <table className="access-table"><thead><tr><th>Администратор</th><th>Состояние</th><th>Создан</th><th>Последний вход</th><th>Действия</th></tr></thead><tbody>{lists.admins?.items.map((admin) => <tr key={admin.id}><td><b>{admin.username}</b>{admin.id === session.admin.id && <small>Текущая учётная запись</small>}</td><td><Status value={admin.status} /></td><td>{date(admin.created_at)}</td><td>{date(admin.last_login_at)}</td><td><button className="button access-danger" disabled={busy || admin.status !== "active"} onClick={() => confirm({ title: "Отключить администратора?", text: `${admin.username} больше не сможет управлять доступом. Активные административные сессии будут завершены. Последнего активного администратора отключить нельзя.`, path: `/access/api/admins/${encodeURIComponent(admin.id)}/deactivate`, reason: false, self: admin.id === session.admin.id, destructive: true })}>Отключить</button></td></tr>)}</tbody></table>}
              {tab === "events" && <table className="access-table access-audit-table"><thead><tr><th>Когда</th><th>Кто / адрес</th><th>Действие</th><th>Объект</th><th>Подробности</th></tr></thead><tbody>{lists.events?.items.map((item) => <tr key={item.id}><td>{date(item.created_at)}</td><td><b>{item.actor || "—"}</b><small>{item.address || "—"}</small></td><td><code>{item.action}</code></td><td><span>{item.target_type || "—"}</span><small>{item.target_id || "—"}</small></td><td><span className="access-event-details">{item.details || "—"}</span></td></tr>)}</tbody></table>}
            </div>
          )}
          {tab !== "admins" && loaded && <Pagination offset={offset} total={total} busy={loading || busy} onChange={(value) => setOffsets((values) => ({ ...values, [tab]: value }))} />}
          {tab === "admins" && <form className="access-admin-form" onSubmit={(event) => void addAdmin(event)}><div><h3>Добавить администратора</h3><p>Передайте пароль новому администратору по защищённому каналу. Он не появится в списке или журнале.</p></div><div className="access-fields"><label>Логин<input required autoComplete="off" minLength={1} maxLength={128} value={newUsername} disabled={busy} onChange={(event) => setNewUsername(event.target.value)} /></label><label>Пароль · минимум 16 символов<input required type="password" autoComplete="new-password" minLength={16} maxLength={1024} value={newPassword} disabled={busy} onChange={(event) => setNewPassword(event.target.value)} /></label><button className="button primary" disabled={busy || !newUsername.trim() || newPassword.length < 16}>{busy ? "Сохраняем…" : "Добавить администратора"}</button></div></form>}
        </section>
      </main>
      {confirmation && <div className="modal-backdrop" onMouseDown={(event) => { if (!busy && event.target === event.currentTarget) setConfirmation(null); }}><section className="modal access-confirm" role="dialog" aria-modal="true" aria-labelledby="access-confirm-title"><header className="modal-header"><h2 id="access-confirm-title">{confirmation.title}</h2><button className="icon-button" aria-label="Закрыть" disabled={busy} onClick={() => setConfirmation(null)}>×</button></header><form className="modal-form" onSubmit={(event) => void applyConfirmation(event)}><p>{confirmation.text}</p>{confirmation.reason && <label>Причина (сохранится в журнале)<textarea autoFocus maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Например: смена роли или прекращение работы с сервисом" disabled={busy} /></label>}{error && <div className="form-error" role="alert">{error}</div>}<div className="modal-actions"><button className="button secondary" type="button" disabled={busy} onClick={() => setConfirmation(null)}>Отмена</button><button className={`button ${confirmation.destructive ? "access-danger" : "primary"}`} disabled={busy}>{busy ? "Сохраняем…" : "Подтвердить"}</button></div></form></section></div>}
    </div>
  );
}
