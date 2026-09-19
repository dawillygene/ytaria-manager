import { Network } from "@capacitor/network";
import { useEffect, useState, useSyncExternalStore } from "react";
import { isNative } from "./platform";
import { session } from "./session";

/** Reachability of *a* network. It says nothing about the API being up; API failures surface as NetworkError. */
export function useOnline(): boolean {
  const [online, setOnline] = useState<boolean>(typeof navigator === "undefined" ? true : navigator.onLine);
  useEffect(() => {
    if (isNative()) {
      let remove: (() => void) | undefined;
      void Network.getStatus().then((s) => setOnline(s.connected));
      void Network.addListener("networkStatusChange", (s) => setOnline(s.connected)).then((h) => { remove = () => void h.remove(); });
      return () => remove?.();
    }
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => { window.removeEventListener("online", on); window.removeEventListener("offline", off); };
  }, []);
  return online;
}

export function useSession() {
  useSyncExternalStore(session.subscribe, session.getSnapshot);
  return session;
}

export type ThemePref = "system" | "light" | "dark";
const THEME_KEY = "ytaria.theme";

export function readTheme(): ThemePref {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(pref: ThemePref): void {
  const root = document.documentElement;
  if (pref === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", pref);
  try {
    localStorage.setItem(THEME_KEY, pref);
  } catch {
    /* preference simply won't persist */
  }
}

/** Debounced local persistence for unsent drafts (never contains credentials). */
export function usePersistentDraft(key: string): [string, (v: string) => void] {
  const [value, setValue] = useState(() => {
    try { return localStorage.getItem(key) ?? ""; } catch { return ""; }
  });
  useEffect(() => {
    const t = setTimeout(() => {
      try {
        if (value) localStorage.setItem(key, value);
        else localStorage.removeItem(key);
      } catch { /* ignore */ }
    }, 250);
    return () => clearTimeout(t);
  }, [key, value]);
  return [value, setValue];
}
