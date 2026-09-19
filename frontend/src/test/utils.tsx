import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";
import type { Job } from "../lib/types";

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

type Handler = (req: { url: URL; method: string; body: any; headers: Headers }) => { status?: number; body?: unknown; headers?: Record<string, string> } | Promise<any>;

/** Minimal fetch router: keys are "METHOD /path" (query string ignored). Records every call. */
export function mockFetch(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; body: any; headers: Headers }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = new Headers(init?.headers as HeadersInit);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url.pathname, body, headers });
    const handler = routes[`${method} ${url.pathname}`];
    if (!handler) return new Response(JSON.stringify({ error: { code: "not_mocked", message: `${method} ${url.pathname}` } }), { status: 404 });
    const out = await handler({ url, method, body, headers });
    return new Response(out.status === 204 ? null : JSON.stringify(out.body ?? {}), { status: out.status ?? 200, headers: { "Content-Type": "application/json", ...out.headers } });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

export function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "11111111-1111-1111-1111-111111111111", url: "https://www.youtube.com/watch?v=abc", host: "www.youtube.com", title: "A video",
    duration_seconds: 125, selection: "best", status: "queued", stage: null, progress_percent: 0, downloaded_bytes: 0, total_bytes: null,
    speed_bps: null, eta_seconds: null, attempt: 0, max_attempts: 3, retry_at: null, error: null, created_at: "2026-01-01T00:00:00Z",
    started_at: null, finished_at: null, expires_at: null, file: null, delivery: { state: "unavailable", saved_at: null },
    actions: { pause: true, resume: false, cancel: true, retry: false, delete: false, download: false }, ...overrides,
  };
}

export const page = (items: Job[], extra: Record<string, unknown> = {}) => ({ items, total: items.length, page: 1, page_size: 20, ...extra });
