import { ChevronLeft, ChevronRight, History } from "lucide-react";
import { useState } from "react";
import { Alert, Button, EmptyState, Spinner } from "../../components/ui";
import { JobCard } from "./JobCard";
import { errorMessage, useHistory } from "./queries";

export function HistoryScreen({ online }: { online: boolean }) {
  const [page, setPage] = useState(1);
  const q = useHistory(page);
  const pages = q.data ? Math.max(1, Math.ceil(q.data.total / q.data.page_size)) : 1;

  return (
    <section aria-labelledby="hist-h" className="space-y-3">
      <h2 id="hist-h" className="text-lg font-semibold">History</h2>
      {q.isPending && <Spinner label="Loading history" />}
      {q.isError && <Alert kind="error" action={<Button onClick={() => void q.refetch()}>Retry</Button>}>{errorMessage(q.error)}</Alert>}
      {q.data && q.data.items.length === 0 && <EmptyState title="No history yet" icon={<History className="size-6" />}>Finished, failed and canceled downloads are listed here.</EmptyState>}
      <ul className="space-y-3">{q.data?.items.map((j) => <JobCard key={j.id} job={j} online={online} />)}</ul>
      {q.data && q.data.total > q.data.page_size && (
        <nav aria-label="History pages" className="flex items-center justify-between gap-3 pt-2">
          <Button icon={<ChevronLeft className="size-4" aria-hidden />} disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Newer</Button>
          <span className="text-sm text-muted" aria-live="polite">Page {page} of {pages}</span>
          <Button disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>Older<ChevronRight className="size-4" aria-hidden /></Button>
        </nav>
      )}
    </section>
  );
}
