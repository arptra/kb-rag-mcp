export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T>(
  path: string,
  password: string,
  init: RequestInit = {},
  timeoutMs = 8000,
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (password) headers.set("X-KB-Admin-Password", password);
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers,
      signal: init.signal || controller.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(`Сервер не ответил за ${Math.round(timeoutMs / 1000)} секунд`, 504);
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
  const text = await response.text();
  let payload: unknown = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { error: text };
    }
  }
  if (!response.ok) {
    const message =
      typeof payload === "object" && payload && "error" in payload
        ? String((payload as { error: unknown }).error)
        : response.statusText;
    throw new ApiError(message, response.status);
  }
  return payload as T;
}

export function post<T>(path: string, password: string, body: unknown, timeoutMs?: number): Promise<T> {
  return api<T>(path, password, { method: "POST", body: JSON.stringify(body) }, timeoutMs);
}

export async function download(path: string, password: string, filename: string): Promise<void> {
  const headers = new Headers();
  if (password) headers.set("X-KB-Admin-Password", password);
  const response = await fetch(path, { headers });
  if (!response.ok) {
    const text = await response.text();
    let message = text || response.statusText;
    try {
      const payload = JSON.parse(text) as { error?: unknown };
      if (payload.error) message = String(payload.error);
    } catch {
      // The response was plain text; keep it as the error message.
    }
    throw new ApiError(message, response.status);
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
