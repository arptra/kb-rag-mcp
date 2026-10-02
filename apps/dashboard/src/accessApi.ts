import { ApiError } from "./api";

/** Session cookies are HttpOnly; only the admin CSRF value lives in JS memory. */
export async function accessApi<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (init.signal?.aborted) controller.abort();
  init.signal?.addEventListener("abort", onAbort, { once: true });
  const timer = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(path, {
      ...init,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    const text = await response.text();
    let payload: unknown = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        // Do not display arbitrary proxy HTML or unexpectedly returned secrets.
        throw new ApiError("Сервер вернул ответ в неожиданном формате", response.status || 502);
      }
    }
    if (!response.ok) {
      const message = payload && typeof payload === "object" && "error" in payload
        ? String(payload.error)
        : `Ошибка запроса (${response.status})`;
      throw new ApiError(message, response.status);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError" && !init.signal?.aborted) {
      throw new ApiError("Сервер не ответил за 15 секунд. Повторите запрос.", 504);
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    init.signal?.removeEventListener("abort", onAbort);
  }
}

export function accessPost<T>(path: string, body: unknown = {}, csrfToken?: string): Promise<T> {
  return accessApi<T>(path, {
    method: "POST",
    headers: csrfToken ? { "X-CSRF-Token": csrfToken } : undefined,
    body: JSON.stringify(body),
  });
}

export function accessError(error: unknown): string {
  return error instanceof Error ? error.message : "Не удалось выполнить запрос";
}
