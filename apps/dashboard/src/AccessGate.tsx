/// <reference types="vite/client" />
import { useCallback, useEffect, useRef, useState } from "react";
import App from "./App";
import { DASHBOARD_ACCESS_EXPIRED, setDashboardAccessMode } from "./api";
import { accessApi, accessError, accessPost } from "./accessApi";
import type { BrowserAccessStatus } from "./accessTypes";

export default function AccessGate() {
  const [status, setStatus] = useState<BrowserAccessStatus | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const generation = useRef(0);

  const check = useCallback(async (afterDenial = false) => {
    const request = ++generation.current;
    setStatus(null);
    setBusy(true);
    setError("");
    try {
      const result = await accessApi<BrowserAccessStatus>("/auth/status");
      if (request !== generation.current) return;
      setDashboardAccessMode(result.enabled);
      if (result.enabled) sessionStorage.removeItem("rag-admin-password");
      // A persistent 403 must not create a status → dashboard → 403 loop.
      // After a denied request, require an explicit user action to reopen it.
      setStatus(afterDenial && result.enabled ? { ...result, authenticated: false } : result);
      if (afterDenial && result.enabled) setError("Доступ к дашборду отклонён. Сессия могла истечь или быть отозвана. Проверьте доступ либо обратитесь к администратору.");
    } catch (caught) {
      if (request === generation.current) setError(accessError(caught));
    } finally {
      if (request === generation.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void check();
    const expired = () => { void check(true); };
    window.addEventListener(DASHBOARD_ACCESS_EXPIRED, expired);
    return () => {
      ++generation.current;
      window.removeEventListener(DASHBOARD_ACCESS_EXPIRED, expired);
    };
  }, [check]);

  const login = async () => {
    setBusy(true);
    setError("");
    try {
      await accessPost("/auth/browser-session");
      await check();
    } catch (caught) {
      setError(accessError(caught));
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    // Keep the protected dashboard unmounted while logout is in flight.
    setStatus((value) => value ? { ...value, authenticated: false } : value);
    setBusy(true);
    setError("");
    try {
      await accessPost("/auth/logout");
      await check();
    } catch (caught) {
      setError(`Не удалось завершить сессию на сервере: ${accessError(caught)}`);
    } finally {
      setBusy(false);
    }
  };

  if (status && (!status.enabled || status.authenticated)) {
    return <App secureMode={status.enabled} accountName={status.user?.common_name || status.user?.subject || status.user?.id} onSessionLogout={() => void logout()} />;
  }

  return (
    <main className="login-shell access-login">
      <section className="login-card">
        <div className="brand-mark large">R</div>
        <span className="eyebrow">Вход по CN сертификата</span>
        <h1>RAG Control Plane</h1>
        {!status ? (
          <p>{busy ? "Проверяем доступ к сервису…" : "Не удалось проверить доступ к сервису."}</p>
        ) : (
          <>
            <p>Выберите ваш существующий личный сертификат в браузере. Имя пользователя берётся из поля CN (Common Name); после входа персональный токен даёт доступ к индексам, инструментам и дашборду.</p>
            {status.user && <div className="access-account-identity"><small>Учётная запись{status.user.common_name ? " · CN" : ""}</small><b>{status.user.common_name || status.user.subject || status.user.id}</b></div>}
            {!status.certificate_present && (
              <div className="access-callout">
                Сервер не получил клиентский сертификат с одним непустым CN. Заново откройте HTTPS-адрес сервиса и выберите уже доступный браузеру личный сертификат. Если выбора нет, уточните у администратора, какой сертификат использовать. Загружать закрытый ключ на эту страницу не нужно.
              </div>
            )}
            {status.certificate_mode === "trusted_ca" && <p className="access-dev-note">На сервере явно включена дополнительная проверка сертификата по доверенному CA.</p>}
            <button className="button primary wide" disabled={busy || !status.certificate_present} onClick={() => void login()}>
              {busy ? "Выполняем вход…" : "Войти по сертификату"}
            </button>
            <small>Одинаковый CN означает одну учётную запись, даже если сертификат или его издатель изменился. CN не даёт роль администратора: управление доступом открывается отдельно по логину и паролю. Отозванный доступ восстанавливает администратор.</small>
            {import.meta.env.DEV && <p className="access-dev-note">Режим разработки: Vite не передаёт клиентский TLS-сертификат. Для входа по сертификату откройте собранный дашборд непосредственно на HTTPS-порту backend.</p>}
          </>
        )}
        {error && <div className="form-error" role="alert">{error}</div>}
        {!busy && <button className="button secondary wide" onClick={() => void check()}>Повторить проверку</button>}
        <div className="connect-gate-links"><a className="access-link" href="/connect">Получить MCP-конфиг →</a><a className="access-link" href="/access-admin">Управление доступом →</a></div>
      </section>
    </main>
  );
}
