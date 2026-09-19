import { ApiError, NetworkError } from "./errors";
import { apiUrl, isNative } from "./platform";
import type { ApiErrorBody } from "./types";
import { session } from "./session";

export { ApiError, NetworkError } from "./errors";

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);
// Codes meaning "the access credential is missing/expired": worth one refresh attempt.
const REFRESHABLE = new Set(["not_authenticated", "invalid_token"]);

export interface RequestOptions {
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** internal: prevents refresh recursion */
  skipRefresh?: boolean;
}

async function send(method: string, path: string, opts: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (isNative()) {
    headers["X-Client"] = "native";
    const token = session.accessToken;
    if (token) headers["Authorization"] = `Bearer ${token}`;
  } else if (UNSAFE.has(method) && session.csrfToken) {
    headers["X-CSRF-Token"] = session.csrfToken;
  }
  try {
    return await fetch(apiUrl(path), {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: isNative() ? "omit" : "include",
      signal: opts.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new NetworkError();
  }
}

async function toError(res: Response): Promise<ApiError> {
  let body: { error?: ApiErrorBody } | undefined;
  try {
    body = await res.json();
  } catch {
    /* non-JSON error (proxy page, etc.) */
  }
  const e = body?.error;
  return new ApiError(res.status, e?.code ?? `http_${res.status}`, e?.message ?? `Request failed (${res.status}).`, e?.fields);
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  let res = await send(method, path, opts);
  if (res.status === 401 && !opts.skipRefresh) {
    const err = await toError(res.clone());
    if (REFRESHABLE.has(err.code)) {
      // Throws NetworkError if offline (session is kept); expires the session only on a definitive 401.
      const ok = await session.refresh();
      if (ok) res = await send(method, path, { ...opts, skipRefresh: true });
    } else if (err.code === "session_revoked" || err.code === "refresh_reuse") {
      session.expire();
    }
  }
  if (res.status === 401 && !opts.skipRefresh) session.expire();
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, opts),
  post: <T>(path: string, body?: unknown, opts: RequestOptions = {}) => request<T>("POST", path, { ...opts, body: body ?? {} }),
  del: <T = void>(path: string, opts?: RequestOptions) => request<T>("DELETE", path, opts),
};

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
