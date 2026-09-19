import { CheckCircle2, Download, ExternalLink, Pause, Play, RotateCcw, Smartphone, Trash2, X } from "lucide-react";
import { useState } from "react";
import { Alert, Badge, Button, Card, ConfirmDialog, ProgressBar } from "../../components/ui";
import { formatBytes, formatDate, formatDuration, formatEta, formatRelativeFuture, formatSpeed } from "../../lib/format";
import { isNative } from "../../lib/platform";
import type { Job, JobStatus } from "../../lib/types";
import { errorMessage, useJobAction, type JobAction } from "./queries";
import { openSaved, startTransfer, useTransferState } from "./transfer";
import { useQueryClient } from "@tanstack/react-query";

const STATUS: Record<JobStatus, { label: string; tone: "neutral" | "accent" | "ok" | "warn" | "danger" | "info" }> = {
  queued: { label: "Queued", tone: "info" },
  running: { label: "Downloading on server", tone: "accent" },
  pausing: { label: "Pausing…", tone: "warn" },
  paused: { label: "Paused", tone: "warn" },
  canceling: { label: "Canceling…", tone: "warn" },
  canceled: { label: "Canceled", tone: "neutral" },
  completed: { label: "Ready on server", tone: "ok" },
  failed: { label: "Failed", tone: "danger" },
  expired: { label: "Expired", tone: "neutral" },
};

const STAGE: Record<string, string> = { starting: "Starting", downloading: "Downloading", merging: "Merging audio and video", finalizing: "Finishing up" };

export function JobCard({ job, online }: { job: Job; online: boolean }) {
  const act = useJobAction();
  const qc = useQueryClient();
  const transfer = useTransferState(job.id);
  const [confirm, setConfirm] = useState<null | "cancel" | "delete">(null);
  const status = STATUS[job.status];
  const busyAction = act.isPending ? act.variables?.action : null;
  const working = job.status === "running" || job.status === "queued" || job.status === "pausing" || job.status === "canceling";
  const title = job.title || job.host; // rendered as text by React, never as HTML

  const run = (action: JobAction) => act.mutate({ id: job.id, action }, { onSuccess: () => setConfirm(null), onError: () => setConfirm(null) });
  const refreshDelivery = () => void qc.invalidateQueries({ queryKey: ["jobs"] });

  return (
    <Card as="li" className="space-y-3" aria-label={title}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="break-words font-medium leading-snug">{title}</h3>
          <p className="truncate text-sm text-muted">
            {job.host}
            {job.duration_seconds ? ` · ${formatDuration(job.duration_seconds)}` : ""}
            {` · ${job.selection.startsWith("audio") ? "Audio" : job.selection === "best" ? "Best quality" : job.selection.slice(1) + "p"}`}
          </p>
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>

      {(job.status === "running" || job.status === "pausing" || job.status === "paused" || (job.status === "queued" && job.progress_percent > 0)) && (
        <div className="space-y-1.5">
          <ProgressBar value={job.progress_percent} label={`Progress for ${title}`} indeterminate={job.status === "running" && job.progress_percent === 0} />
          <p className="flex flex-wrap justify-between gap-x-3 text-xs text-muted">
            <span>{job.stage ? STAGE[job.stage] ?? job.stage : ""}{job.progress_percent > 0 ? ` · ${Math.round(job.progress_percent)}%` : ""}</span>
            <span>{[formatBytes(job.downloaded_bytes) + (job.total_bytes ? ` of ${formatBytes(job.total_bytes)}` : ""), formatSpeed(job.speed_bps), formatEta(job.eta_seconds)].filter(Boolean).join(" · ")}</span>
          </p>
        </div>
      )}

      {job.status === "queued" && job.retry_at && (
        <Alert kind="warning">
          Attempt {job.attempt} of {job.max_attempts} hit a problem{job.error ? `: ${job.error.message}` : "."} Retrying in about {formatRelativeFuture(job.retry_at)}.
        </Alert>
      )}
      {job.status === "queued" && !job.retry_at && <p className="text-sm text-muted">Waiting for a free download slot on the server.</p>}
      {job.status === "failed" && job.error && (
        <Alert kind="error">
          <p className="font-medium">{job.error.message}</p>
          {job.attempt > 1 && <p className="text-xs opacity-80">Tried {job.attempt} times.</p>}
        </Alert>
      )}
      {job.status === "expired" && <p className="text-sm text-muted">The file was removed from the server after the retention period. You can download it again.</p>}
      {act.isError && <Alert kind="error">{errorMessage(act.error)}</Alert>}

      {job.status === "completed" && job.file && (
        <div className="space-y-2 rounded-lg bg-surface2 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="min-w-0 break-all">{job.file.name} · {formatBytes(job.file.size)}</span>
            <DeliveryBadge job={job} />
          </div>
          {transfer.phase === "downloading" || transfer.phase === "saving" || transfer.phase === "preparing" ? (
            <div className="space-y-1">
              <ProgressBar value={transfer.total ? (transfer.bytes / transfer.total) * 100 : 0} label={`Transfer of ${title}`} indeterminate={!transfer.total || transfer.phase !== "downloading"} />
              <p className="text-xs text-muted" role="status">
                {transfer.phase === "preparing" ? "Preparing transfer…" : transfer.phase === "saving" ? "Saving to Downloads…" : `${formatBytes(transfer.bytes)} of ${formatBytes(transfer.total)}`}
                {isNative() && " · keep the app open until the transfer finishes"}
              </p>
            </div>
          ) : (
            <>
              {transfer.phase === "error" && <Alert kind="error">{transfer.error}</Alert>}
              <div className="flex flex-wrap gap-2">
                {job.actions.download && (
                  <Button variant="primary" icon={isNative() ? <Smartphone className="size-4" aria-hidden /> : <Download className="size-4" aria-hidden />} disabled={!online}
                    onClick={() => void startTransfer(job, refreshDelivery)}>
                    {transfer.phase === "error" ? "Try again" : isNative() ? (job.delivery.state === "saved" ? "Save again" : "Save to this device") : "Download to this device"}
                  </Button>
                )}
                {isNative() && transfer.phase === "done" && transfer.uri && (
                  <Button icon={<ExternalLink className="size-4" aria-hidden />} onClick={() => void openSaved(job, transfer.uri!)}>Open</Button>
                )}
              </div>
              {!online && <p className="text-xs text-muted">Reconnect to transfer this file.</p>}
              {job.expires_at && <p className="text-xs text-muted">Kept on the server until {formatDate(job.expires_at)}.</p>}
            </>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {job.actions.pause && <Button icon={<Pause className="size-4" aria-hidden />} onClick={() => run("pause")} loading={busyAction === "pause"} disabled={!online}>Pause</Button>}
        {job.actions.resume && <Button icon={<Play className="size-4" aria-hidden />} onClick={() => run("resume")} loading={busyAction === "resume"} disabled={!online}>Resume</Button>}
        {job.actions.retry && <Button variant={job.status === "failed" ? "primary" : "secondary"} icon={<RotateCcw className="size-4" aria-hidden />} onClick={() => run("retry")} loading={busyAction === "retry"} disabled={!online}>{job.status === "expired" ? "Download again" : "Retry"}</Button>}
        {job.actions.cancel && <Button variant="danger" icon={<X className="size-4" aria-hidden />} onClick={() => setConfirm("cancel")} disabled={!online || job.status === "canceling"}>Cancel</Button>}
        {job.actions.delete && <Button variant="ghost" icon={<Trash2 className="size-4" aria-hidden />} onClick={() => setConfirm("delete")} disabled={!online}>Remove</Button>}
      </div>
      {working && job.status !== "running" && <p className="sr-only" role="status">{status.label}</p>}

      <ConfirmDialog
        open={confirm === "cancel"} danger title="Cancel this download?" confirmLabel="Cancel download" busy={act.isPending}
        onCancel={() => setConfirm(null)} onConfirm={() => run("cancel")}
      >The server will stop downloading and discard the partial file.</ConfirmDialog>
      <ConfirmDialog
        open={confirm === "delete"} danger title="Remove from your list?" confirmLabel="Remove" busy={act.isPending}
        onCancel={() => setConfirm(null)} onConfirm={() => run("delete")}
      >This deletes the file from the server. Anything already saved on your device stays there.</ConfirmDialog>
    </Card>
  );
}

function DeliveryBadge({ job }: { job: Job }) {
  const d = job.delivery.state;
  if (d === "saved") return <Badge tone="ok"><CheckCircle2 className="mr-1 size-3" aria-hidden />Saved on device</Badge>;
  if (d === "transferring") return <Badge tone="info">Transferring…</Badge>;
  if (d === "sent") return <Badge tone="info">Sent to browser</Badge>;
  return <Badge tone="neutral">Not on your device yet</Badge>;
}
