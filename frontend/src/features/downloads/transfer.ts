/**
 * Server -> device transfer. "Ready on server" is not "Saved on device": this module owns the second half.
 *
 * Web:     ask for a short-lived ticket and let the browser's download manager stream the file.
 *          The server can tell us it *sent* all bytes; only the browser knows if the user saved it.
 * Android: FOREGROUND transfer. @capacitor/filesystem downloads natively (streams to disk, no JS memory
 *          copy) into the app cache, then a small native plugin copies it into the public Downloads
 *          collection via MediaStore (no storage permission needed on Android 10+).
 *          This does NOT continue if Android kills the app in the background: the user simply retries.
 *          Server-side processing is independent and keeps running.
 */
import { Directory, Filesystem } from "@capacitor/filesystem";
import { registerPlugin } from "@capacitor/core";
import { useSyncExternalStore } from "react";
import { api } from "../../lib/api";
import { apiUrl, isNative } from "../../lib/platform";
import type { Job, Ticket } from "../../lib/types";
import { ApiError, NetworkError } from "../../lib/errors";

export interface YtariaFilesPlugin {
  saveToDownloads(o: { path: string; displayName: string; mimeType: string }): Promise<{ uri: string }>;
  openFile(o: { uri: string; mimeType: string }): Promise<void>;
}
export const YtariaFiles = registerPlugin<YtariaFilesPlugin>("YtariaFiles");

export type Phase = "idle" | "preparing" | "downloading" | "saving" | "done" | "error";
export interface TransferState { phase: Phase; bytes: number; total: number; error?: string; uri?: string }

const IDLE: TransferState = { phase: "idle", bytes: 0, total: 0 };
const states = new Map<string, TransferState>();
const listeners = new Set<() => void>();
const set = (id: string, s: TransferState) => { states.set(id, s); listeners.forEach((l) => l()); };

export function useTransferState(id: string): TransferState {
  return useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => states.get(id) ?? IDLE);
}
export const resetTransferStates = () => { states.clear(); listeners.forEach((l) => l()); };

function describe(err: unknown): string {
  if (err instanceof ApiError || err instanceof NetworkError) return err.message;
  // Native plugin errors can contain internal file paths; log them, never show them.
  console.warn("transfer failed", err);
  return "Saving to this device failed. Check your connection and free space, then try again.";
}

export function safeFileName(name: string): string {
  return name.replace(/[\\/:*?"<>|\x00-\x1f]+/g, "_").slice(0, 120) || "download";
}

const inFlight = new Set<string>();

export async function startTransfer(job: Job, onSaved: () => void): Promise<void> {
  if (inFlight.has(job.id)) return; // one transfer per job at a time
  inFlight.add(job.id);
  set(job.id, { phase: "preparing", bytes: 0, total: job.file?.size ?? 0 });
  let progressHandle: { remove: () => Promise<void> } | undefined;
  try {
    const ticket = await api.post<Ticket>(`/api/jobs/${job.id}/download-ticket`);
    const url = apiUrl(ticket.url);
    if (!isNative()) {
      const a = document.createElement("a");
      a.href = url;
      a.download = safeFileName(job.file?.name ?? "download");
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
      set(job.id, { phase: "done", bytes: job.file?.size ?? 0, total: job.file?.size ?? 0 });
      setTimeout(onSaved, 1500); // let the server record the streamed bytes, then refresh delivery state
      return;
    }
    const name = safeFileName(job.file?.name ?? "download");
    const cachePath = `ytaria/${job.id}.download`;
    progressHandle = await Filesystem.addListener("progress", (p) => {
      if (p.url === url) set(job.id, { phase: "downloading", bytes: p.bytes, total: p.contentLength || (job.file?.size ?? 0) });
    });
    set(job.id, { phase: "downloading", bytes: 0, total: job.file?.size ?? 0 });
    // downloadFile does not create missing parent directories.
    await Filesystem.mkdir({ path: "ytaria", directory: Directory.Cache, recursive: true }).catch(() => undefined);
    const res = await Filesystem.downloadFile({ url, path: cachePath, directory: Directory.Cache, progress: true, recursive: true });
    if (!res.path) throw new Error("download produced no file");
    set(job.id, { phase: "saving", bytes: job.file?.size ?? 0, total: job.file?.size ?? 0 });
    const saved = await YtariaFiles.saveToDownloads({ path: res.path, displayName: name, mimeType: job.file?.mime ?? "application/octet-stream" });
    await Filesystem.deleteFile({ path: cachePath, directory: Directory.Cache }).catch(() => undefined);
    set(job.id, { phase: "done", bytes: job.file?.size ?? 0, total: job.file?.size ?? 0, uri: saved.uri });
    await api.post(`/api/jobs/${job.id}/transfer`, { state: "saved" }).catch(() => undefined);
    onSaved();
  } catch (err) {
    set(job.id, { phase: "error", bytes: 0, total: 0, error: describe(err) });
    if (isNative()) {
      await Filesystem.deleteFile({ path: `ytaria/${job.id}.download`, directory: Directory.Cache }).catch(() => undefined);
      await api.post(`/api/jobs/${job.id}/transfer`, { state: "failed" }).catch(() => undefined);
    }
  } finally {
    inFlight.delete(job.id);
    void progressHandle?.remove();
  }
}

export async function openSaved(job: Job, uri: string): Promise<void> {
  await YtariaFiles.openFile({ uri, mimeType: job.file?.mime ?? "*/*" });
}
