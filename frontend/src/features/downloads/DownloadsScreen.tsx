import { Inbox } from "lucide-react";
import { Alert, Button, EmptyState, Spinner } from "../../components/ui";
import { JobCard } from "./JobCard";
import { SubmitPanel } from "./SubmitPanel";
import { errorMessage, useActiveJobs, useReadyJobs } from "./queries";

export function DownloadsScreen({ online }: { online: boolean }) {
  const active = useActiveJobs();
  const ready = useReadyJobs();
  const activeItems = active.data?.items ?? [];
  const readyItems = ready.data?.items ?? [];

  return (
    <div className="space-y-6">
      <SubmitPanel online={online} onQueued={() => void active.refetch()} />

      <section aria-labelledby="active-h" className="space-y-3">
        <h2 id="active-h" className="text-lg font-semibold">Active queue</h2>
        {active.isPending ? <Spinner label="Loading your queue" /> : null}
        {active.isError && (
          <Alert kind="error" action={<Button onClick={() => void active.refetch()}>Retry</Button>}>
            {errorMessage(active.error)} {active.data ? "Showing the last known state." : ""}
          </Alert>
        )}
        {active.data && activeItems.length === 0 && (
          <EmptyState title="Nothing downloading" icon={<Inbox className="size-6" />}>Paste a link above to start a download on the server.</EmptyState>
        )}
        <ul className="space-y-3">{activeItems.map((j) => <JobCard key={j.id} job={j} online={online} />)}</ul>
      </section>

      <section aria-labelledby="ready-h" className="space-y-3">
        <h2 id="ready-h" className="text-lg font-semibold">Ready to save</h2>
        {ready.isPending ? <Spinner label="Loading completed downloads" /> : null}
        {ready.isError && <Alert kind="error">{errorMessage(ready.error)}</Alert>}
        {ready.data && readyItems.length === 0 && <p className="text-sm text-muted">Finished downloads appear here until they expire.</p>}
        <ul className="space-y-3">{readyItems.map((j) => <JobCard key={j.id} job={j} online={online} />)}</ul>
      </section>
    </div>
  );
}
