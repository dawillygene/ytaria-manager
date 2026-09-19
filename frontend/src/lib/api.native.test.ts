import { beforeEach, describe, expect, it, vi } from "vitest";

const store = new Map<string, string>();
vi.mock("./platform", () => ({ isNative: () => true, API_BASE: "https://api.example.com", apiUrl: (p: string) => `https://api.example.com${p}` }));
vi.mock("@aparajita/capacitor-secure-storage", () => ({
  SecureStorage: {
    get: vi.fn(async (k: string) => store.get(k) ?? null),
    set: vi.fn(async (k: string, v: string) => { store.set(k, v); }),
    remove: vi.fn(async (k: string) => { store.delete(k); }),
  },
}));

import { SecureStorage } from "@aparajita/capacitor-secure-storage";
import { api } from "./api";
import { session } from "./session";
import { mockFetch } from "../test/utils";

const user = { id: "u1", email: "a@b.co", display_name: "a" };
beforeEach(() => { store.clear(); session.resetForTests(); });

describe("api client (Android / Capacitor)", () => {
  it("keeps the refresh token only in secure storage and sends a bearer access token", async () => {
    const { calls, fn } = mockFetch({
      "POST /api/auth/login": () => ({ body: { user, access_token: "acc-1", refresh_token: "ref-1", expires_in: 900 } }),
      "GET /api/usage": () => ({ body: {} }),
    });
    await session.authenticate("/api/auth/login", { email: "a@b.co", password: "x" });
    expect(SecureStorage.set).toHaveBeenCalledWith("refresh_token", "ref-1", false, false);
    expect(store.get("refresh_token")).toBe("ref-1");
    expect(localStorage.length).toBe(0);
    await api.get("/api/usage");
    const usage = calls.find((c) => c.path === "/api/usage")!;
    expect(usage.headers.get("Authorization")).toBe("Bearer acc-1");
    expect(usage.headers.get("X-Client")).toBe("native");
    expect(usage.headers.get("X-CSRF-Token")).toBeNull();
    expect((fn.mock.calls.at(-1)![1] as RequestInit).credentials).toBe("omit");
  });

  it("boots from the stored refresh token and rotates it", async () => {
    store.set("refresh_token", "ref-old");
    const { calls } = mockFetch({ "POST /api/auth/refresh": () => ({ body: { user, access_token: "acc-2", refresh_token: "ref-new" } }) });
    await session.bootstrap();
    expect(calls[0]!.body).toEqual({ refresh_token: "ref-old" });
    expect(session.state).toBe("signed-in");
    expect(store.get("refresh_token")).toBe("ref-new");
  });

  it("stays signed in (with the stored token) when offline at start-up... and signs out only on rejection", async () => {
    store.set("refresh_token", "ref-old");
    mockFetch({ "POST /api/auth/refresh": () => { throw new TypeError("offline"); } });
    await session.bootstrap();
    expect(session.state).toBe("unreachable");                     // not "signed-out"
    expect(store.get("refresh_token")).toBe("ref-old");           // kept for the next launch
    mockFetch({ "POST /api/auth/refresh": () => ({ status: 401, body: { error: { code: "invalid_token", message: "" } } }) });
    session.resetForTests();
    await session.bootstrap();
    expect(store.has("refresh_token")).toBe(false);
    expect(session.state).toBe("signed-out");
  });

  it("logout revokes server-side and wipes the secure token", async () => {
    store.set("refresh_token", "ref-1");
    const { calls } = mockFetch({ "POST /api/auth/logout": () => ({ status: 204 }) });
    await session.logout();
    expect(calls[0]!.body).toEqual({ refresh_token: "ref-1" });
    expect(store.size).toBe(0);
  });
});
