export type JobStatus =
  | "queued" | "running" | "pausing" | "paused" | "canceling" | "canceled" | "completed" | "failed" | "expired";

export interface User { id: string; email: string; display_name: string }
export interface AuthResponse {
  user: User;
  csrf_token?: string | null;
  access_token?: string | null;
  refresh_token?: string | null;
  expires_in?: number | null;
}
export interface PublicConfig { app: string; registration: "open" | "invite" | "closed"; password_min_length: number }
export interface ApiErrorBody { code: string; message: string; fields?: { field: string; message: string }[] }

export interface QualityOption {
  id: string; label: string; kind: "video" | "audio"; height: number | null; ext: string; estimated_bytes: number | null;
}
export interface Inspection {
  id: string; status: "pending" | "running" | "succeeded" | "failed"; url: string; title: string | null;
  duration_seconds: number | null; extractor: string | null; options: QualityOption[];
  error: { code: string; message: string } | null; expires_at: string;
}
export interface Delivery { state: "unavailable" | "ready" | "transferring" | "sent" | "saved"; saved_at: string | null }
export interface Job {
  id: string; url: string; host: string; title: string | null; duration_seconds: number | null; selection: string;
  status: JobStatus; stage: string | null; progress_percent: number; downloaded_bytes: number; total_bytes: number | null;
  speed_bps: number | null; eta_seconds: number | null; attempt: number; max_attempts: number; retry_at: string | null;
  error: { code: string; message: string } | null; created_at: string; started_at: string | null; finished_at: string | null;
  expires_at: string | null; file: { name: string; size: number; mime: string } | null; delivery: Delivery;
  actions: { pause: boolean; resume: boolean; cancel: boolean; retry: boolean; delete: boolean; download: boolean };
}
export interface JobPage { items: Job[]; total: number; page: number; page_size: number }
export interface Usage {
  used_bytes: number; quota_bytes: number; active_jobs: number; max_active_jobs: number;
  completed_retention_hours: number; max_file_bytes: number; supported_sites: string[];
}
export interface Ticket { url: string; expires_at: string }
