import { beforeEach, describe, expect, it } from "vitest";
import { api } from "./api";
import { ApiError, NetworkError } from "./errors";
import { session } from "./session";
import { mockFetch } from "../test/utils";

const user = { id: "u1", email: "a@b.co", display_name: "a" };

beforeEach(() => {
  session.resetForTests();
  session.state = "signed-in";
  session.user = user;
  session.csrfToken = "csrf-1";
});

describe("api client (web)", () => {
  it("sends the CSRF header on unsafe methods only and includes cookies", async () => {
    const { calls, fn } = mockFetch({ "GET /api/x": () => ({ body: {} }), "POST /api/x": () => ({ body: {} }) });
    await api.get("/api/x");
    await api.post("/api/x", { a: 1 });
    expect(calls[0]!.headers.get("X-CSRF-Token")).toBeNull();
    expect(calls[1]!.headers.get("X-CSRF-Token")).toBe("csrf-1");
    expect((fn.mock.calls[1]![1] as RequestInit).credentials).toBe("include");
    expect(calls[1]!.headers.get("Authorization")).toBeNull();
  });

  it("refreshes once on an expired access token and retries the request", async () => {
    let first = true;
    const { calls } = mockFetch({
      "GET /api/jobs": () => { if (first) { first = false; return { status: 401, body: { error: { code: "invalid_token", message: "expired" } } }; } return { body: { ok: 1 } }; },
      "POST /api/auth/refresh": () => ({ body: { user, csrf_token: "csrf-2" } }),
    });
    await expect(api.get("/api/jobs")).resolves.toEqual({ ok: 1 });
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual(["GET /api/jobs", "POST /api/auth/refresh", "GET /api/jobs"]);
    expect(calls[1]!.headers.get("X-Requested-With")).toBe("ytaria-web");
    expect(session.csrfToken).toBe("csrf-2");
    expect(session.state).toBe("signed-in");
  });

  it("concurrent 401s trigger a single refresh", async () => {
    let refreshes = 0;
    let expired = true;
    mockFetch({
      "GET /api/a": () => expired ? { status: 401, body: { error: { code: "invalid_token", message: "" } } } : { body: {} },
      "GET /api/b": () => expired ? { status: 401, body: { error: { code: "invalid_token", message: "" } } } : { body: {} },
      "POST /api/auth/refresh": async () => { refreshes++; await new Promise((r) => setTimeout(r, 20)); expired = false; return { body: { user, csrf_token: "c" } }; },
    });
    await Promise.all([api.get("/api/a"), api.get("/api/b")]);
    expect(refreshes).toBe(1);
  });

  it("signs out only when the server definitively rejects the refresh token", async () => {
    mockFetch({
      "GET /api/jobs": () => ({ status: 401, body: { error: { code: "invalid_token", message: "" } } }),
      "POST /api/auth/refresh": () => ({ status: 401, body: { error: { code: "invalid_token", message: "expired" } } }),
    });
    await expect(api.get("/api/jobs")).rejects.toBeInstanceOf(ApiError);
    expect(session.state).toBe("signed-out");
  });

  it("does NOT sign out on network failure, or on a server error during refresh", async () => {
    mockFetch({ "GET /api/jobs": () => { throw new TypeError("Failed to fetch"); } });
    await expect(api.get("/api/jobs")).rejects.toBeInstanceOf(NetworkError);
    expect(session.state).toBe("signed-in");

    mockFetch({
      "GET /api/jobs": () => ({ status: 401, body: { error: { code: "invalid_token", message: "" } } }),
      "POST /api/auth/refresh": () => ({ status: 503, body: { error: { code: "x", message: "down" } } }),
    });
    await expect(api.get("/api/jobs")).rejects.toBeInstanceOf(NetworkError);
    expect(session.state).toBe("signed-in");
  });

  it("a revoked session signs out immediately without a refresh attempt", async () => {
    const { calls } = mockFetch({ "GET /api/jobs": () => ({ status: 401, body: { error: { code: "session_revoked", message: "" } } }) });
    await expect(api.get("/api/jobs")).rejects.toBeInstanceOf(ApiError);
    expect(session.state).toBe("signed-out");
    expect(calls.some((c) => c.path === "/api/auth/refresh")).toBe(false);
  });

  it("surfaces structured server errors", async () => {
    mockFetch({ "POST /api/jobs": () => ({ status: 429, body: { error: { code: "queue_limit", message: "Too many active downloads." } } }) });
    await expect(api.post("/api/jobs", {})).rejects.toMatchObject({ status: 429, code: "queue_limit", message: "Too many active downloads." });
  });

  it("never persists credentials to web storage", async () => {
    mockFetch({ "POST /api/auth/login": () => ({ body: { user, csrf_token: "secret-csrf" } }) });
    await session.authenticate("/api/auth/login", { email: "a@b.co", password: "pw" });
    expect(JSON.stringify({ ...localStorage })).not.toContain("secret-csrf");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
