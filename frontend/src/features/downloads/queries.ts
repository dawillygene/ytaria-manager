import { keepPreviousData, useMutation, useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, newIdempotencyKey } from "../../lib/api";
import { ApiError, NetworkError } from "../../lib/errors";
import type { Inspection, Job, JobPage, PublicConfig, Usage } from "../../lib/types";

export const qk = {
  config: ["config"] as const,
  usage: ["usage"] as const,
  active: ["jobs", "active"] as const,
  ready: ["jobs", "ready"] as const,
  history: (page: number) => ["jobs", "history", page] as const,
  inspection: (id: string | null) => ["inspection", id] as const,
};

/** Poll faster while work is in flight; back off exponentially (max 30s) while requests are failing. */
export function backoffInterval(base: number, failures: number): number {
  return Math.min(30_000, base * 2 ** Math.min(failures, 5));
}

/** Retry only transient failures (network / 5xx). Auth and validation errors are not retried. */
export function shouldRetry(count: number, err: unknown): boolean {
  if (count >= 2) return false;
  if (err instanceof NetworkError) return true;
  return err instanceof ApiError && err.status >= 500;
}

export function useConfig() {
  return useQuery({ queryKey: qk.config, queryFn: () => api.get<PublicConfig>("/api/config"), staleTime: 5 * 60_000 });
}

export function useUsage() {
  return useQuery({ queryKey: qk.usage, queryFn: () => api.get<Usage>("/api/usage"), staleTime: 15_000 });
}

const pollWhileWorking = (q: Query<JobPage>): number | false => {
  const items = q.state.data?.items ?? [];
  if (items.length === 0 && q.state.status !== "error") return false;
  return backoffInterval(2000, q.state.fetchFailureCount);
};

export function useActiveJobs() {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: qk.active,
    queryFn: () => api.get<JobPage>("/api/jobs?scope=active&page_size=50"),
    refetchInterval: pollWhileWorking,
    refetchIntervalInBackground: false,
  });
  // When something leaves the active list it finished (or was canceled): refresh the other views once.
  const previous = useRef<Set<string>>(new Set());
  useEffect(() => {
    const ids = new Set((query.data?.items ?? []).map((j) => j.id));
    if ([...previous.current].some((id) => !ids.has(id))) {
      void qc.invalidateQueries({ queryKey: ["jobs", "ready"] });
      void qc.invalidateQueries({ queryKey: ["jobs", "history"] });
      void qc.invalidateQueries({ queryKey: qk.usage });
    }
    previous.current = ids;
  }, [query.data, qc]);
  return query;
}

export function useReadyJobs() {
  return useQuery({
    queryKey: qk.ready,
    queryFn: () => api.get<JobPage>("/api/jobs?scope=ready&page_size=50"),
    // Delivery state changes while a transfer runs; poll lightly only if a transfer may be in progress.
    refetchInterval: (q) => (q.state.data?.items.some((j) => j.delivery.state === "transferring") ? 3000 : false),
  });
}

export function useHistory(page: number) {
  return useQuery({
    queryKey: qk.history(page),
    queryFn: () => api.get<JobPage>(`/api/jobs?scope=history&page=${page}&page_size=10`),
    placeholderData: keepPreviousData,
  });
}

/** Start an inspection and poll it until it settles. */
export function useInspection(id: string | null) {
  return useQuery({
    queryKey: qk.inspection(id),
    enabled: !!id,
    queryFn: () => api.get<Inspection>(`/api/inspections/${id}`),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      if (s === "succeeded" || s === "failed") return false;
      return backoffInterval(1200, q.state.fetchFailureCount);
    },
  });
}

export function useStartInspection() {
  return useMutation({ mutationFn: (url: string) => api.post<Inspection>("/api/inspections", { url }) });
}

/**
 * Creating a job is protected against duplicates twice: the button is disabled while the request is in
 * flight, and every attempt carries an Idempotency-Key that the server enforces. The key is stable for a
 * given (inspection, quality) so a retry after a network failure cannot create a second job.
 */
export function useCreateJob() {
  const qc = useQueryClient();
  const keys = useRef(new Map<string, string>());
  return useMutation({
    mutationFn: ({ inspectionId, selection }: { inspectionId: string; selection: string }) => {
      const k = `${inspectionId}:${selection}`;
      let key = keys.current.get(k);
      if (!key) { key = newIdempotencyKey(); keys.current.set(k, key); }
      return api.post<Job>("/api/jobs", { inspection_id: inspectionId, selection }, { headers: { "Idempotency-Key": key } });
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["jobs"] });
      void qc.invalidateQueries({ queryKey: qk.usage });
    },
  });
}

export type JobAction = "pause" | "resume" | "cancel" | "retry" | "delete";

export function useJobAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, action }: { id: string; action: JobAction }): Promise<void> => {
      if (action === "delete") await api.del(`/api/jobs/${id}`);
      else await api.post<Job>(`/api/jobs/${id}/${action}`);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["jobs"] });
      void qc.invalidateQueries({ queryKey: qk.usage });
    },
  });
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError || err instanceof NetworkError) return err.message;
  return "Something went wrong. Please try again.";
}
