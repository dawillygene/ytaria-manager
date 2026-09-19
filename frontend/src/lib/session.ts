/**
 * Session management shared by web and Capacitor.
 *
 * Web:     HttpOnly cookies hold the credentials (JavaScript never sees them). Only the CSRF token
 *          (derived server-side from the session) is kept, in memory.
 * Android: the short-lived access token lives in memory; the refresh token lives in Android
 *          Keystore-backed storage (@aparajita/capacitor-secure-storage). Nothing sensitive is written
 *          to localStorage or Capacitor Preferences.
 */
import { SecureStorage } from "@aparajita/capacitor-secure-storage";
import { ApiError, NetworkError } from "./errors";
import { apiUrl, isNative } from "./platform";
import type { AuthResponse, User } from "./types";

const REFRESH_KEY = "refresh_token";
type Listener = () => void;

export type SessionState = "loading" | "signed-out" | "signed-in" | "unreachable";

class Session {
  accessToken: string | null = null;
  csrfToken: string | null = null;
  user: User | null = null;
  state: SessionState = "loading";
  private listeners = new Set<Listener>();
  private refreshing: Promise<boolean> | null = null;

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };
  getSnapshot = (): string => `${this.state}:${this.user?.id ?? ""}`;
  private emit() {
    this.listeners.forEach((l) => l());
  }

  private async post(path: string, body: unknown, extra: Record<string, string> = {}): Promise<Response> {
    const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json", ...extra };
    if (isNative()) headers["X-Client"] = "native";
    try {
      return await fetch(apiUrl(path), {
        method: "POST", headers, body: JSON.stringify(body), credentials: isNative() ? "omit" : "include", cache: "no-store",
      });
    } catch {
      throw new NetworkError();
    }
  }

  private async adopt(auth: AuthResponse) {
    this.user = auth.user;
    this.csrfToken = auth.csrf_token ?? null;
    this.accessToken = auth.access_token ?? null;
    if (isNative() && auth.refresh_token) await SecureStorage.set(REFRESH_KEY, auth.refresh_token, false, false);
    this.state = "signed-in";
    this.emit();
  }

  /** Called once at start-up. Never signs the user out because of a network failure. */
  async bootstrap(): Promise<void> {
    if (this.state === "unreachable") { this.state = "loading"; this.emit(); }
    try {
      if (isNative()) {
        const stored = await SecureStorage.get(REFRESH_KEY, false, false).catch(() => null);
        if (!stored) return this.setSignedOut();
        if (await this.refresh()) return;
        return; // refresh() already decided (expired vs. offline)
      }
      const res = await fetch(apiUrl("/api/auth/me"), { credentials: "include", cache: "no-store", headers: { Accept: "application/json" } });
      if (res.ok) return void (await this.adopt((await res.json()) as AuthResponse));
      if (res.status === 401 && (await this.refresh())) return;
      this.setSignedOut();
    } catch {
      // Offline / server down at launch: we cannot know whether the session is valid. Do not sign out
      // and do not drop the stored refresh token; let the user retry.
      this.state = "unreachable";
      this.emit();
    }
  }
  /** Test seam: returns the singleton to its initial, signed-out state. */
  resetForTests(): void {
    this.accessToken = this.csrfToken = this.user = null;
    this.state = "loading";
    this.refreshing = null;
  }

  private setSignedOut() {
    this.user = null;
    this.accessToken = null;
    this.csrfToken = null;
    this.state = "signed-out";
    this.emit();
  }

  /** Server rejected the session definitively (not a network problem). */
  expire = (): void => {
    if (isNative()) void SecureStorage.remove(REFRESH_KEY, false).catch(() => undefined);
    clearDrafts();
    this.setSignedOut();
  };

  /**
   * Rotate credentials. Single-flight within this tab, and across tabs on the web (Web Locks),
   * because the server rotates refresh tokens and a lost race would look like token reuse.
   * Returns true on success, false on definitive rejection (session expired), throws NetworkError offline.
   */
  refresh(): Promise<boolean> {
    if (!this.refreshing) {
      this.refreshing = this.doRefreshLocked().finally(() => {
        this.refreshing = null;
      });
    }
    return this.refreshing;
  }

  private async doRefreshLocked(): Promise<boolean> {
    if (!isNative() && typeof navigator !== "undefined" && "locks" in navigator) {
      return navigator.locks.request("ytaria-refresh", () => this.doRefresh());
    }
    return this.doRefresh();
  }

  private async doRefresh(): Promise<boolean> {
    for (let attempt = 0; attempt < 2; attempt++) {
      let res: Response;
      if (isNative()) {
        const token = await SecureStorage.get(REFRESH_KEY, false, false).catch(() => null);
        if (typeof token !== "string" || !token) return this.fail();
        res = await this.post("/api/auth/refresh", { refresh_token: token });
      } else {
        res = await this.post("/api/auth/refresh", {}, { "X-Requested-With": "ytaria-web" });
      }
      if (res.ok) {
        await this.adopt((await res.json()) as AuthResponse);
        return true;
      }
      const code = (await res.json().catch(() => ({})))?.error?.code;
      if (res.status === 401 && code === "refresh_conflict" && attempt === 0) {
        await new Promise((r) => setTimeout(r, 300)); // another tab just rotated; the new cookie is in place
        continue;
      }
      if (res.status >= 500 || res.status === 429) {
        throw new NetworkError(); // server trouble is not a reason to sign the user out
      }
      return this.fail();
    }
    return this.fail();
  }

  private fail(): boolean {
    this.expire();
    return false;
  }

  async authenticate(path: "/api/auth/login" | "/api/auth/register", body: Record<string, string>): Promise<void> {
    const res = await this.post(path, body);
    if (!res.ok) {
      const e = (await res.json().catch(() => ({})))?.error ?? {};
      throw new ApiError(res.status, e.code ?? `http_${res.status}`, e.message ?? "Request failed.", e.fields);
    }
    await this.adopt((await res.json()) as AuthResponse);
  }

  async logout(): Promise<void> {
    try {
      const token = isNative() ? await SecureStorage.get(REFRESH_KEY, false, false).catch(() => null) : null;
      await this.post("/api/auth/logout", isNative() ? { refresh_token: token } : {});
    } catch {
      /* best effort: local state is cleared regardless; the server-side session expires on its own */
    }
    this.expire();
  }
}

export const session = new Session();

export const DRAFT_KEY = "ytaria.draft.url";
export function clearDrafts(): void {
  try {
    localStorage.removeItem(DRAFT_KEY);
  } catch {
    /* storage unavailable */
  }
}
