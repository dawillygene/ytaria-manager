const UNITS = ["B", "KB", "MB", "GB", "TB"];

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  let value = Math.max(0, bytes);
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${UNITS[unit]}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const mm = String(m).padStart(h ? 2 : 1, "0");
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function formatEta(seconds: number | null | undefined): string {
  if (seconds == null) return "";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s left`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min left`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m left`;
}

export function formatSpeed(bps: number | null | undefined): string {
  return bps ? `${formatBytes(bps)}/s` : "";
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function formatRelativeFuture(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const s = Math.max(0, Math.round((new Date(iso).getTime() - now) / 1000));
  if (s < 60) return `${s}s`;
  return `${Math.round(s / 60)} min`;
}
