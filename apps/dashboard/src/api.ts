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
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (password) headers.set("X-KB-Admin-Password", password);
  const response = await fetch(path, {
    ...init,
    headers,
  });
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

export function post<T>(path: string, password: string, body: unknown): Promise<T> {
  return api<T>(path, password, { method: "POST", body: JSON.stringify(body) });
}
