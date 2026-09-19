import { App as CapApp } from "@capacitor/app";
import { Download, History, Settings, WifiOff } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Spinner } from "./components/ui";
import { AuthScreen } from "./features/auth/AuthScreen";
import { DownloadsScreen } from "./features/downloads/DownloadsScreen";
import { HistoryScreen } from "./features/downloads/HistoryScreen";
import { SettingsScreen } from "./features/settings/SettingsScreen";
import { cx } from "./lib/cx";
import { useOnline, useSession } from "./lib/hooks";
import { isNative } from "./lib/platform";

type Tab = "downloads" | "history" | "settings";
const TABS: { id: Tab; label: string; Icon: typeof Download }[] = [
  { id: "downloads", label: "Downloads", Icon: Download },
  { id: "history", label: "History", Icon: History },
  { id: "settings", label: "Settings", Icon: Settings },
];

export default function App() {
  const session = useSession();
  useEffect(() => { void session.bootstrap(); }, [session]);
  if (session.state === "loading") return <div className="grid min-h-dvh place-items-center"><Spinner label="Starting" /></div>;
  if (session.state === "unreachable") {
    return (
      <div className="safe-x grid min-h-dvh place-items-center">
        <div className="max-w-sm space-y-4 text-center">
          <WifiOff className="mx-auto size-8 text-muted" aria-hidden />
          <h1 className="text-xl font-semibold">Can't reach the server</h1>
          <p className="text-sm text-muted">You're still signed in. Check your connection, then try again.</p>
          <Button variant="primary" onClick={() => void session.bootstrap()}>Try again</Button>
        </div>
      </div>
    );
  }
  if (session.state === "signed-out") return <AuthScreen />;
  return <Shell />;
}

function Shell() {
  const online = useOnline();
  const [tab, setTab] = useState<Tab>("downloads");

  // Android hardware/gesture back: return to the main tab first, leave the app only from there.
  const onBack = useCallback(() => {
    const open = document.querySelector("dialog[open]");
    if (open) { open.dispatchEvent(new Event("cancel", { cancelable: true })); return; }
    if (tab !== "downloads") setTab("downloads");
    else void CapApp.exitApp();
  }, [tab]);
  useEffect(() => {
    if (!isNative()) return;
    const handle = CapApp.addListener("backButton", onBack);
    return () => { void handle.then((h) => h.remove()); };
  }, [onBack]);

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="safe-top safe-x sticky top-0 z-10 border-b border-line bg-surface/95 backdrop-blur-none">
        <div className="mx-auto flex h-14 w-full max-w-3xl items-center justify-between">
          <h1 className="text-lg font-semibold">ytaria-manager</h1>
          <nav aria-label="Main" className="hidden gap-1 sm:flex">
            {TABS.map(({ id, label, Icon }) => (
              <button key={id} type="button" onClick={() => setTab(id)} aria-current={tab === id ? "page" : undefined}
                className={cx("flex min-h-11 items-center gap-2 rounded-lg px-3 text-sm font-medium", tab === id ? "bg-accentsoft text-accent" : "text-muted hover:bg-surface2")}>
                <Icon className="size-4" aria-hidden />{label}
              </button>
            ))}
          </nav>
        </div>
      </header>
      {!online && (
        <div className="safe-x mx-auto w-full max-w-3xl pt-3">
          <Alert kind="warning"><span className="flex items-center gap-2"><WifiOff className="size-4" aria-hidden />You're offline. Showing the last information we loaded; downloads and controls need a connection. You'll stay signed in.</span></Alert>
        </div>
      )}
      <main className="safe-x mx-auto w-full max-w-3xl flex-1 py-4 pb-24 sm:pb-8">
        {tab === "downloads" && <DownloadsScreen online={online} />}
        {tab === "history" && <HistoryScreen online={online} />}
        {tab === "settings" && <SettingsScreen />}
      </main>
      <nav aria-label="Main" className="safe-bottom fixed inset-x-0 bottom-0 z-10 grid grid-cols-3 border-t border-line bg-surface sm:hidden">
        {TABS.map(({ id, label, Icon }) => (
          <button key={id} type="button" onClick={() => setTab(id)} aria-current={tab === id ? "page" : undefined}
            className={cx("flex min-h-14 flex-col items-center justify-center gap-0.5 text-xs font-medium", tab === id ? "text-accent" : "text-muted")}>
            <Icon className="size-5" aria-hidden />{label}
          </button>
        ))}
      </nav>
    </div>
  );
}
