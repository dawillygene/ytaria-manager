import { LogOut } from "lucide-react";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, ProgressBar, Spinner } from "../../components/ui";
import { formatBytes } from "../../lib/format";
import { applyTheme, readTheme, useSession, type ThemePref } from "../../lib/hooks";
import { isNative } from "../../lib/platform";
import { errorMessage, useUsage } from "../downloads/queries";

export function SettingsScreen() {
  const session = useSession();
  const qc = useQueryClient();
  const usage = useUsage();
  const [theme, setTheme] = useState<ThemePref>(readTheme);
  const [busy, setBusy] = useState(false);

  async function logout() {
    setBusy(true);
    await session.logout();
    qc.clear();
  }
  const u = usage.data;
  const pct = u ? (u.used_bytes / Math.max(1, u.quota_bytes)) * 100 : 0;

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">Settings</h2>
      <Card aria-label="Account" className="space-y-1">
        <h3 className="font-medium">Account</h3>
        <p className="break-all text-sm text-muted">{session.user?.email}</p>
      </Card>

      <Card aria-label="Storage" className="space-y-3">
        <h3 className="font-medium">Server storage</h3>
        {usage.isPending && <Spinner label="Loading usage" />}
        {usage.isError && <Alert kind="error">{errorMessage(usage.error)}</Alert>}
        {u && (
          <>
            <ProgressBar value={pct} label="Storage used" />
            <p className="text-sm text-muted">{formatBytes(u.used_bytes)} of {formatBytes(u.quota_bytes)} used · {u.active_jobs} of {u.max_active_jobs} active downloads</p>
            <ul className="list-disc space-y-1 pl-5 text-sm text-muted">
              <li>Finished files are kept on the server for {Math.round(u.completed_retention_hours / 24)} days, then removed.</li>
              <li>Largest single file: {formatBytes(u.max_file_bytes)}.</li>
              <li>Supported sites: {u.supported_sites.join(", ")}.</li>
            </ul>
          </>
        )}
      </Card>

      <Card aria-label="Appearance" className="space-y-2">
        <h3 className="font-medium">Appearance</h3>
        <div role="radiogroup" aria-label="Theme" className="flex flex-wrap gap-2">
          {(["system", "light", "dark"] as const).map((t) => (
            <label key={t} className={`flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border px-3 text-sm ${theme === t ? "border-accent bg-accentsoft" : "border-line"}`}>
              <input type="radio" name="theme" className="accent-[var(--accent)]" checked={theme === t} onChange={() => { setTheme(t); applyTheme(t); }} />
              {t[0]!.toUpperCase() + t.slice(1)}
            </label>
          ))}
        </div>
      </Card>

      <Card aria-label="About" className="space-y-1 text-sm text-muted">
        <h3 className="font-medium text-fg">About</h3>
        <p>ytaria-manager {isNative() ? "for Android" : "web"}. Downloads run on the server; saving to this device is a separate step.</p>
        <p>Developed and maintained by <a className="text-accent underline" href="https://www.dawillygene.com/" target="_blank" rel="noopener noreferrer">Elia William Mariki (dawillygene)</a>.</p>
      </Card>

      <Button variant="danger" className="w-full" icon={<LogOut className="size-4" aria-hidden />} loading={busy} onClick={() => void logout()}>Sign out</Button>
    </div>
  );
}
