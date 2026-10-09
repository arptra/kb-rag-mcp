/// <reference types="vite/client" />
import { useCallback, useEffect, useRef, useState } from "react";
import { accessApi, accessError } from "./accessApi";
import type { AccessUser, BrowserAccessStatus } from "./accessTypes";

type McpServerName = "corporate-kb" | "corporate-skills";

interface McpConnection {
  httpUrl: string;
  headers: { Authorization: string };
}

interface PersonalMcpConfig {
  config: {
    mcpServers: {
      "corporate-kb": McpConnection;
      "corporate-skills"?: McpConnection;
    };
  };
  expires_at: number;
  user: AccessUser;
}

export default function McpConnectApp() {
  const [status, setStatus] = useState<BrowserAccessStatus | null>(null);
  const [result, setResult] = useState<PersonalMcpConfig | null>(null);
  const [selectedServers, setSelectedServers] = useState<McpServerName[]>(["corporate-kb", "corporate-skills"]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const configField = useRef<HTMLTextAreaElement | null>(null);
  const https = window.location.protocol === "https:";
  const selectedConfig = result ? {
    mcpServers: Object.fromEntries(Object.entries(result.config.mcpServers).filter(([name]) => selectedServers.includes(name as McpServerName))),
  } : null;
  const hasSelection = !!selectedConfig && Object.keys(selectedConfig.mcpServers).length > 0;
  const toggleServer = (name: McpServerName) => setSelectedServers(current => current.includes(name) ? current.filter(item => item !== name) : [...current, name]);

  const check = useCallback(async () => {
    const request = ++generation.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setResult(null);
    setStatus(null);
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await accessApi<BrowserAccessStatus>("/auth/status", { signal: controller.signal });
      if (request === generation.current) setStatus(response);
    } catch (caught) {
      if (request === generation.current) setError(accessError(caught));
    } finally {
      if (request === generation.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void check();
    const hide = () => {
      ++generation.current;
      pending.current?.abort();
      setResult(null);
      setBusy(false);
      setError("");
      setNotice("Конфиг скрыт при уходе со страницы. При необходимости получите его снова. Это не отзывает уже выданный токен.");
    };
    const visibility = () => { if (document.visibilityState === "hidden") hide(); };
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", hide);
    return () => {
      ++generation.current;
      pending.current?.abort();
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", hide);
    };
  }, [check]);

  useEffect(() => {
    if (!result) return;
    let timer = 0;
    const expire = () => {
      const remaining = result.expires_at * 1000 - Date.now();
      if (remaining <= 0) {
        setResult(null);
        setNotice("Срок действия токена истёк. Нажмите «Получить MCP-конфиг» и обновите подключённые корпоративные MCP в настройках клиента.");
      } else {
        timer = window.setTimeout(expire, Math.min(remaining, 2_147_483_647));
      }
    };
    expire();
    return () => window.clearTimeout(timer);
  }, [result]);

  const issue = async () => {
    if (busy || !https || !status?.enabled || !status.certificate_present) return;
    const request = ++generation.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setResult(null);
    setError("");
    setNotice("");
    try {
      // Never issue on mount, status refresh, or StrictMode effect replay.
      const response = await accessApi<PersonalMcpConfig>("/auth/mcp-config", {
        method: "POST",
        body: JSON.stringify({}),
        signal: controller.signal,
      });
      if (request !== generation.current || document.visibilityState === "hidden") return;
      if (!Number.isFinite(response.expires_at) || response.expires_at * 1000 <= Date.now()) {
        throw new Error("Сервер вернул истёкший срок токена. Повторите запрос или обратитесь к администратору.");
      }
      setSelectedServers(response.config.mcpServers["corporate-skills"] ? ["corporate-kb", "corporate-skills"] : ["corporate-kb"]);
      setResult(response);
    } catch (caught) {
      if (request === generation.current) setError(accessError(caught));
    } finally {
      if (request === generation.current) setBusy(false);
    }
  };

  const copy = async () => {
    if (!result || !selectedConfig || !hasSelection || result.expires_at * 1000 <= Date.now()) return;
    const request = generation.current;
    setError("");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(JSON.stringify(selectedConfig, null, 2));
      if (request === generation.current) setNotice(`Конфиг скопирован. Добавьте выбранные подключения (${Object.keys(selectedConfig.mcpServers).join(", ")}) в mcpServers вашего клиента; остальные подключения сохраните.`);
    } catch {
      if (request !== generation.current) return;
      configField.current?.focus();
      configField.current?.select();
      setError("Браузер не разрешил копирование. JSON выделен — скопируйте его вручную сочетанием Ctrl+C или ⌘C.");
    }
  };

  const download = () => {
    if (!result || !selectedConfig || !hasSelection || result.expires_at * 1000 <= Date.now()) return;
    const file = new Blob([`${JSON.stringify(selectedConfig, null, 2)}\n`], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(file);
    const anchor = document.createElement("a");
    try {
      anchor.href = url;
      anchor.download = "corporate-mcp.json";
      document.body.appendChild(anchor);
      anchor.click();
      setNotice("Скачивание corporate-mcp.json начато. Файл содержит выбранные подключения и секрет: храните его локально и не добавляйте в Git.");
    } finally {
      anchor.remove();
      // Let the browser begin the user-requested download before releasing the URL.
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  };

  const canIssue = https && status?.enabled && status.certificate_present && !busy;
  const configText = selectedConfig ? JSON.stringify(selectedConfig, null, 2) : "";

  return (
    <div className="access-shell connect-shell">
      <header className="access-header">
        <a className="access-brand" href="/admin"><span className="brand-mark">R</span><span><b>RAG</b><small>ПОДКЛЮЧЕНИЕ MCP</small></span></a>
        <div className="access-header-actions"><a className="button secondary" href="/admin">Дашборд ↗</a><a className="button secondary" href="/access-admin">Управление доступом</a></div>
      </header>
      <main className="access-main connect-main">
        <div className="access-intro"><div><span className="eyebrow">Персональное подключение</span><h1>Подключить MCP</h1><p>Выберите существующий личный сертификат в браузере и получите конфиг для AI-клиента. Учётная запись определяется по CN (Common Name), без Python и локальных скриптов.</p></div></div>
        <ol className="connect-steps" aria-label="Порядок подключения"><li><span>1</span><div><b>Выберите личный сертификат</b><small>Сервис прочитает имя пользователя из CN.</small></div></li><li><span>2</span><div><b>Получите персональный конфиг</b><small>Токен появится только после нажатия кнопки.</small></div></li><li><span>3</span><div><b>Добавьте выбранные MCP</b><small>База знаний и скиллы подключаются отдельно.</small></div></li></ol>
        <section className="access-section connect-panel" aria-labelledby="connect-access-title">
          <div className="access-section-head"><div><h2 id="connect-access-title">Проверка доступа</h2><p>Вход в дашборд не заменяет личный сертификат при получении MCP-конфига.</p></div><span className={`access-status ${canIssue || result ? "active" : "expired"}`}>{busy ? "Проверяем…" : !https ? "Нужен HTTPS" : !status ? "Нет статуса" : !status.enabled ? "Отключено" : status.certificate_present ? "Сертификат с CN получен" : "Нужен сертификат с CN"}</span></div>
          <div className="connect-access-body">
            {!https && <div className="access-callout">Страница открыта без HTTPS. Откройте HTTPS-адрес сервиса напрямую. Получение персонального конфига по HTTP недоступно.</div>}
            {status && !status.enabled && <div className="access-callout">Выдача персональных конфигов отключена в настройках сервера. Попросите администратора включить управление доступом или выдать инструкции подключения для вашего сервиса.</div>}
            {status?.enabled && !status.certificate_present && <div className="access-callout">Сервер не получил личный сертификат с одним непустым CN. Заново откройте HTTPS-адрес сервиса и выберите уже доступный браузеру сертификат. Если выбора нет, уточните у администратора, какой сертификат использовать. Не загружайте закрытый ключ на эту страницу.</div>}
            {status?.certificate_mode === "trusted_ca" && <p className="connect-dev-note">На сервере явно включена дополнительная проверка сертификата по доверенному CA.</p>}
            {status?.user && <div className="access-account-identity"><small>Учётная запись{status.user.common_name ? " · CN" : ""}</small><b>{status.user.common_name || status.user.subject || status.user.id}</b></div>}
            {import.meta.env.DEV && <p className="connect-dev-note">Предпросмотр Vite не передаёт клиентский TLS-сертификат. Для получения конфига используйте собранную страницу /connect непосредственно на HTTPS-порту backend.</p>}
            <div className="connect-actions"><button className="button primary" disabled={!canIssue} onClick={() => void issue()}>{busy ? "Подождите…" : "Получить MCP-конфиг"}</button><button className="button secondary" disabled={busy} onClick={() => void check()}>Повторить проверку</button></div>
            <p className="connect-help">Одинаковый CN означает одну учётную запись независимо от издателя и отпечатка сертификата. Токен действует до своего срока истечения или отзыва. После истечения получите конфиг снова и обновите выбранные подключения в клиенте. Заблокированного пользователя может восстановить только администратор.</p>
          </div>
          {error && <div className="form-error" role="alert">{error}</div>}
          {notice && <div className="access-notice" role="status">{notice}</div>}
        </section>
        {result && <section className="access-section connect-panel" aria-labelledby="connect-config-title">
          <div className="access-section-head"><div><h2 id="connect-config-title">Ваш MCP-конфиг</h2><div className="access-account-identity"><small>Учётная запись{result.user.common_name ? " · CN" : ""}</small><b>{result.user.common_name || result.user.subject || result.user.id}</b></div></div></div>
          <div className="connect-config-body">
            <details className="connect-certificate-details"><summary>Последний сертификат · справочные данные</summary><p>Subject: {result.user.subject || "—"}</p><p>Издатель: {result.user.issuer || "—"}</p><p>SHA-256: <code>{result.user.fingerprint || "—"}</code></p><small>Эти сведения не определяют учётную запись или роль администратора.</small></details>
            <div className="connect-secret-warning"><b>Внутри — секретный токен доступа.</b><span>Не отправляйте этот JSON в чат, не публикуйте его и не коммитьте в Git. Скачанный файл и содержимое буфера обмена остаются у вас даже после закрытия страницы.</span></div>
            <p className="connect-expiry">Действует до <time dateTime={new Date(result.expires_at * 1000).toISOString()}>{new Date(result.expires_at * 1000).toLocaleString("ru-RU")}</time> · время вашего устройства</p>
            <div className="connect-merge-note connect-server-selection" role="group" aria-label="Сервисы для подключения">
              <b>Выберите MCP-серверы</b>
              <p>
                <label>
                  <input
                    type="checkbox"
                    checked={selectedServers.includes("corporate-kb")}
                    onChange={() => toggleServer("corporate-kb")}
                  />
                  <span>База знаний — corporate-kb</span>
                </label>
              </p>
              {result.config.mcpServers["corporate-skills"] && <p>
                <label>
                  <input
                    type="checkbox"
                    checked={selectedServers.includes("corporate-skills")}
                    onChange={() => toggleServer("corporate-skills")}
                  />
                  <span>Скиллы — corporate-skills</span>
                </label>
              </p>}
              <p>{result.config.mcpServers["corporate-skills"]
                ? "Это два отдельных MCP с разными инструментами. Можно подключить оба или выбрать только скиллы. Авторизация уже добавлена в каждую запись."
                : "Сервер скиллов отключён администратором; доступна база знаний."}</p>
              {!hasSelection && <p role="status">Выберите хотя бы один сервер для копирования или скачивания.</p>}
            </div>
            <label className="connect-json-label" htmlFor="personal-mcp-config">JSON для MCP-клиента</label>
            <textarea ref={configField} id="personal-mcp-config" className="connect-json" value={configText} readOnly spellCheck={false} autoComplete="off" aria-describedby="connect-merge-note" />
            <div className="connect-actions"><button className="button primary" disabled={!hasSelection} onClick={() => void copy()}>Скопировать JSON</button><button className="button secondary" disabled={!hasSelection} onClick={download}>Скачать corporate-mcp.json</button><button className="button quiet" onClick={() => { ++generation.current; setResult(null); setNotice("Конфиг скрыт. Токен остаётся действительным; для отзыва обратитесь к администратору."); }}>Скрыть конфиг</button></div>
            <div className="connect-merge-note" id="connect-merge-note"><b>Не заменяйте весь файл настроек клиента.</b><p>Добавьте или обновите выбранные записи <code>corporate-kb</code>{result.config.mcpServers["corporate-skills"] && <> и <code>corporate-skills</code></>} внутри <code>mcpServers</code>. Другие серверы и настройки оставьте без изменений. Затем сохраните настройки и переподключите MCP в клиенте.</p><p>Эта страница не изменяет файлы на вашем компьютере автоматически. Формат конфигурации поддерживается клиентами с полями <code>mcpServers</code>, <code>httpUrl</code> и <code>headers</code>.</p></div>
            <p className="connect-help">Конфиг хранится только в памяти этой страницы и скрывается при переключении вкладки, уходе со страницы, повторной проверке или истечении токена. Скрытие и выход из дашборда не отзывают скопированный или скачанный MCP-токен. Для отзыва отдельного токена или блокировки всего доступа обратитесь к администратору.</p>
          </div>
        </section>}
      </main>
    </div>
  );
}
