"use strict";

(() => {
  async function request(path, options = {}) {
    const headers = new Headers(options.headers);
    if (options.method && options.method !== "GET") headers.set("Content-Type", "application/json");
    let password;
    try { password = sessionStorage.getItem("rag-admin-password"); } catch { /* Private browsing. */ }
    if (password) headers.set("X-KB-Admin-Password", password);
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    if (response.ok) return response;
    let message;
    if (response.status === 401) message = "Войдите в портал и обновите страницу.";
    else if (response.status === 403) message = "Для этого действия требуется дополнительный доступ.";
    else {
      const body = await response.text();
      if (response.headers.get("Content-Type")?.includes("application/json")) {
        try {
          const data = JSON.parse(body);
          message = typeof data.detail === "string" ? data.detail : data.message;
        } catch { /* Some proxies return a different response format. */ }
      } else if (!body.trim().startsWith("<")) message = body;
      if (typeof message !== "string" || message.length > 600) message = "";
    }
    const error = new Error(message || "Ошибка сервера (" + response.status + ").");
    error.status = response.status;
    throw error;
  }
  async function adminHeaders() {
    const session = await fetch("/access/api/session", { credentials: "same-origin", cache: "no-store" });
    if (session.ok) {
      const payload = await session.json();
      if (payload.csrf_token) return { "X-CSRF-Token": payload.csrf_token };
    }
    return {};
  }
  let adminLogin;
  function adminLoginUrl() {
    if (!adminLogin) adminLogin = fetch("/auth/status", { credentials: "same-origin", cache: "no-store" })
      .then(response => response.ok ? response.json() : {})
      .then(info => info.enabled ? "/access-admin" : "/admin")
      .catch(() => "/admin");
    return adminLogin;
  }
  window.PortalApi = { request, adminHeaders, adminLoginUrl };
})();
