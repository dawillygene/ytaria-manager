import { Search } from "lucide-react";
import { useEffect, useId, useState, type FormEvent } from "react";
import { Alert, Button, Card, Spinner, TextField } from "../../components/ui";
import { formatBytes, formatDuration } from "../../lib/format";
import { usePersistentDraft } from "../../lib/hooks";
import { DRAFT_KEY } from "../../lib/session";
import type { QualityOption } from "../../lib/types";
import { errorMessage, useCreateJob, useInspection, useStartInspection } from "./queries";

export function SubmitPanel({ online, onQueued }: { online: boolean; onQueued: () => void }) {
  const [url, setUrl] = usePersistentDraft(DRAFT_KEY);
  const [inspectionId, setInspectionId] = useState<string | null>(null);
  const [selection, setSelection] = useState<string>("");
  const start = useStartInspection();
  const inspection = useInspection(inspectionId);
  const create = useCreateJob();
  const legend = useId();

  const data = inspection.data;
  const options = data?.status === "succeeded" ? data.options : [];
  useEffect(() => {
    if (options.length && !options.some((o) => o.id === selection)) setSelection(options[0]!.id);
  }, [options, selection]);

  function inspect(e: FormEvent) {
    e.preventDefault();
    if (!online || start.isPending) return;
    create.reset();
    setInspectionId(null);
    start.mutate(url.trim(), { onSuccess: (r) => setInspectionId(r.id) });
  }

  function queue() {
    if (!inspectionId || !selection || create.isPending) return;
    create.mutate({ inspectionId, selection }, {
      onSuccess: () => { setUrl(""); setInspectionId(null); setSelection(""); onQueued(); },
    });
  }

  const checking = start.isPending || (!!inspectionId && (!data || data.status === "pending" || data.status === "running"));

  return (
    <Card aria-label="Add a download" className="space-y-4">
      <form onSubmit={inspect} className="space-y-3" noValidate>
        <TextField
          label="Media link" type="url" inputMode="url" autoComplete="off" autoCapitalize="none" spellCheck={false}
          placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)}
          hint="Only download content you are allowed to save."
        />
        <Button type="submit" variant="primary" icon={<Search className="size-4" aria-hidden />} loading={checking} disabled={!online || !url.trim()}>
          Check link
        </Button>
        {!online && <p className="text-sm text-muted">You're offline. Your link is saved as a draft; reconnect to check it.</p>}
      </form>

      {start.isError && <Alert kind="error">{errorMessage(start.error)}</Alert>}
      {checking && <Spinner label="Checking the link" />}
      {inspection.isError && <Alert kind="error">{errorMessage(inspection.error)}</Alert>}
      {data?.status === "failed" && <Alert kind="error">{data.error?.message ?? "This link can't be downloaded."}</Alert>}

      {data?.status === "succeeded" && (
        <div className="space-y-4 border-t border-line pt-4">
          <div>
            <p className="break-words font-medium">{data.title}</p>
            <p className="text-sm text-muted">{[data.extractor, data.duration_seconds ? formatDuration(data.duration_seconds) : ""].filter(Boolean).join(" · ")}</p>
          </div>
          <fieldset className="space-y-2">
            <legend id={legend} className="mb-1 text-sm font-medium">Quality</legend>
            <div role="radiogroup" aria-labelledby={legend} className="grid gap-2 sm:grid-cols-2">
              {options.map((o) => <QualityChoice key={o.id} option={o} checked={selection === o.id} onSelect={() => setSelection(o.id)} />)}
            </div>
          </fieldset>
          {create.isError && <Alert kind="error">{errorMessage(create.error)}</Alert>}
          <Button variant="primary" onClick={queue} loading={create.isPending} disabled={!online || !selection}>Download to server</Button>
          <p className="text-xs text-muted">The server downloads the file first; you then save it to this device.</p>
        </div>
      )}
    </Card>
  );
}

function QualityChoice({ option, checked, onSelect }: { option: QualityOption; checked: boolean; onSelect: () => void }) {
  return (
    <label className={`flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 ${checked ? "border-accent bg-accentsoft" : "border-line bg-surface"}`}>
      <input type="radio" name="quality" className="size-4 accent-[var(--accent)]" checked={checked} onChange={onSelect} />
      <span className="flex-1 text-sm font-medium">{option.label}</span>
      <span className="text-xs text-muted">{option.estimated_bytes ? `≈ ${formatBytes(option.estimated_bytes)}` : option.ext === "auto" ? "Original format" : option.ext.toUpperCase()}</span>
    </label>
  );
}
