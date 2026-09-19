import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { session } from "../../lib/session";
import { job, mockFetch, page, renderWithClient } from "../../test/utils";
import { backoffInterval, shouldRetry } from "./queries";
import { ApiError, NetworkError } from "../../lib/errors";
import { DownloadsScreen } from "./DownloadsScreen";
import { HistoryScreen } from "./HistoryScreen";
import { JobCard } from "./JobCard";
import { resetTransferStates } from "./transfer";

const inspectionOk = {
  id: "insp-1", status: "succeeded", url: "https://www.youtube.com/watch?v=abc", title: "A <b>bold</b> title", duration_seconds: 125, extractor: "Youtube",
  options: [
    { id: "best", label: "Best available (1080p)", kind: "video", height: 1080, ext: "mp4", estimated_bytes: null },
    { id: "h720", label: "720p", kind: "video", height: 720, ext: "mp4", estimated_bytes: 50_000_000 },
  ], error: null, expires_at: "2099-01-01T00:00:00Z",
};

beforeEach(() => {
  session.resetForTests();
  session.state = "signed-in";
  session.csrfToken = "csrf";
  resetTransferStates();
});

describe("submit flow", () => {
  it("inspects a link, offers server-provided qualities and queues with an idempotency key", async () => {
    const jobs: unknown[] = [];
    const { calls } = mockFetch({
      "POST /api/inspections": () => ({ status: 202, body: { ...inspectionOk, status: "pending", options: [], title: null } }),
      "GET /api/inspections/insp-1": () => ({ body: inspectionOk }),
      "POST /api/jobs": () => { jobs.push(1); return { status: 201, body: job() }; },
      "GET /api/jobs": () => ({ body: page([]) }),
    });
    const user = userEvent.setup();
    renderWithClient(<DownloadsScreen online />);
    await user.type(screen.getByLabelText("Media link"), "https://www.youtube.com/watch?v=abc");
    await user.click(screen.getByRole("button", { name: "Check link" }));
    expect(await screen.findByText("720p")).toBeInTheDocument();
    // untrusted title is shown as text, never parsed as HTML
    expect(screen.getByText("A <b>bold</b> title")).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
    await user.click(screen.getByRole("radio", { name: /720p/ }));
    await user.click(screen.getByRole("button", { name: "Download to server" }));
    await waitFor(() => expect(jobs).toHaveLength(1));
    const create = calls.find((c) => c.method === "POST" && c.path === "/api/jobs")!;
    expect(create.body).toEqual({ inspection_id: "insp-1", selection: "h720" });
    expect(create.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    await waitFor(() => expect(screen.getByLabelText("Media link")).toHaveValue(""));    // draft cleared after success
  });

  it("double-clicking submit sends a single request", async () => {
    let resolve!: () => void;
    const gate = new Promise<void>((r) => { resolve = r; });
    const { calls } = mockFetch({
      "POST /api/inspections": () => ({ status: 202, body: { ...inspectionOk } }),
      "GET /api/inspections/insp-1": () => ({ body: inspectionOk }),
      "POST /api/jobs": async () => { await gate; return { status: 201, body: job() }; },
      "GET /api/jobs": () => ({ body: page([]) }),
    });
    const user = userEvent.setup();
    renderWithClient(<DownloadsScreen online />);
    await user.type(screen.getByLabelText("Media link"), "https://www.youtube.com/watch?v=abc");
    await user.click(screen.getByRole("button", { name: "Check link" }));
    const submit = await screen.findByRole("button", { name: "Download to server" });
    await user.dblClick(submit);
    resolve();
    await waitFor(() => expect(calls.filter((c) => c.method === "POST" && c.path === "/api/jobs")).toHaveLength(1));
  });

  it("reuses the same Idempotency-Key when retrying after a network failure", async () => {
    let attempt = 0;
    const { calls } = mockFetch({
      "POST /api/inspections": () => ({ status: 202, body: inspectionOk }),
      "GET /api/inspections/insp-1": () => ({ body: inspectionOk }),
      "POST /api/jobs": () => { if (attempt++ === 0) throw new TypeError("Failed to fetch"); return { status: 200, body: job() }; },
      "GET /api/jobs": () => ({ body: page([]) }),
    });
    const user = userEvent.setup();
    renderWithClient(<DownloadsScreen online />);
    await user.type(screen.getByLabelText("Media link"), "https://www.youtube.com/watch?v=abc");
    await user.click(screen.getByRole("button", { name: "Check link" }));
    await user.click(await screen.findByRole("button", { name: "Download to server" }));
    expect(await screen.findByText(/Can't reach the server/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Download to server" }));
    await waitFor(() => expect(calls.filter((c) => c.path === "/api/jobs" && c.method === "POST")).toHaveLength(2));
    const keys = calls.filter((c) => c.path === "/api/jobs" && c.method === "POST").map((c) => c.headers.get("Idempotency-Key"));
    expect(keys[0]).toBe(keys[1]);
  });

  it("shows a clear message when the server rejects a link", async () => {
    mockFetch({
      "POST /api/inspections": () => ({ status: 422, body: { error: { code: "url_host_not_supported", message: "That site is not supported yet." } } }),
      "GET /api/jobs": () => ({ body: page([]) }),
    });
    const user = userEvent.setup();
    renderWithClient(<DownloadsScreen online />);
    await user.type(screen.getByLabelText("Media link"), "https://example.com/x");
    await user.click(screen.getByRole("button", { name: "Check link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That site is not supported yet.");
  });

  it("offline: keeps the draft, disables server actions and explains why", async () => {
    mockFetch({ "GET /api/jobs": ({ url }) => ({ body: page(url.searchParams.get("scope") === "active" ? [job({ status: "running", progress_percent: 40, stage: "downloading" })] : []) }) });
    const user = userEvent.setup();
    renderWithClient(<DownloadsScreen online={false} />);
    await user.type(screen.getByLabelText("Media link"), "https://www.youtube.com/watch?v=draft");
    expect(screen.getByRole("button", { name: "Check link" })).toBeDisabled();
    expect(screen.getByText(/You're offline/)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Pause" })).toBeDisabled();       // cached info still visible
    await waitFor(() => expect(localStorage.getItem("ytaria.draft.url")).toBe("https://www.youtube.com/watch?v=draft"));
  });

  it("renders the active queue with progress and stage", async () => {
    mockFetch({ "GET /api/jobs": ({ url }) => ({ body: page(url.searchParams.get("scope") !== "active" ? [] : [job({ status: "running", progress_percent: 42.4, stage: "downloading", downloaded_bytes: 42_000_000, total_bytes: 100_000_000, speed_bps: 2_000_000, eta_seconds: 30 })]) }) });
    renderWithClient(<DownloadsScreen online />);
    const bar = await screen.findByRole("progressbar", { name: /Progress for A video/ });
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByText(/1\.9 MB\/s/)).toBeInTheDocument();
    expect(screen.getByText("Downloading on server")).toBeInTheDocument();
  });
});

describe("job card", () => {
  it("renders hostile titles and errors as plain text", () => {
    const evil = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    renderWithClient(<ul><JobCard online job={job({ title: evil, status: "failed", error: { code: "failed", message: evil }, actions: { pause: false, resume: false, cancel: false, retry: true, delete: true, download: false } })} /></ul>);
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getAllByText(evil).length).toBeGreaterThan(0);
  });

  it("only offers the actions the server allows, and shows failure with retry", async () => {
    const calls = mockFetch({ "POST /api/jobs/11111111-1111-1111-1111-111111111111/retry": () => ({ body: job() }), "GET /api/jobs": () => ({ body: page([]) }) }).calls;
    const user = userEvent.setup();
    renderWithClient(<ul><JobCard online job={job({ status: "failed", attempt: 3, error: { code: "network", message: "A network error interrupted the download." }, actions: { pause: false, resume: false, cancel: false, retry: true, delete: true, download: false } })} /></ul>);
    expect(screen.getByRole("alert")).toHaveTextContent("A network error interrupted the download.");
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/retry"))).toBe(true));
  });

  it("asks for confirmation before canceling", async () => {
    const { calls } = mockFetch({ "POST /api/jobs/11111111-1111-1111-1111-111111111111/cancel": () => ({ body: job({ status: "canceling" }) }), "GET /api/jobs": () => ({ body: page([]) }) });
    const user = userEvent.setup();
    renderWithClient(<ul><JobCard online job={job({ status: "running", progress_percent: 10 })} /></ul>);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(calls.some((c) => c.path.endsWith("/cancel"))).toBe(false);
    const dialog = await screen.findByRole("dialog", { name: "Cancel this download?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel download" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/cancel"))).toBe(true));
  });

  it("separates 'ready on server' from 'saved on device'", () => {
    const base = { status: "completed" as const, file: { name: "a.mp4", size: 1000, mime: "video/mp4" }, actions: { pause: false, resume: false, cancel: false, retry: false, delete: true, download: true } };
    const { unmount } = renderWithClient(<ul><JobCard online job={job({ ...base, delivery: { state: "ready", saved_at: null } })} /></ul>);
    expect(screen.getByText("Ready on server")).toBeInTheDocument();
    expect(screen.getByText("Not on your device yet")).toBeInTheDocument();
    expect(screen.queryByText("Saved on device")).toBeNull();
    unmount();
    renderWithClient(<ul><JobCard online job={job({ ...base, delivery: { state: "saved", saved_at: "2026-01-01T00:00:00Z" } })} /></ul>);
    expect(screen.getByText("Saved on device")).toBeInTheDocument();
  });

  it("web download asks for a ticket then hands the URL to the browser", async () => {
    const clicks: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this.getAttribute("href") ?? ""); });
    const { calls } = mockFetch({
      "POST /api/jobs/11111111-1111-1111-1111-111111111111/download-ticket": () => ({ body: { url: "/api/downloads/tok_abc", expires_at: "2099-01-01T00:00:00Z" } }),
      "GET /api/jobs": () => ({ body: page([]) }),
    });
    const user = userEvent.setup();
    renderWithClient(<ul><JobCard online job={job({ status: "completed", file: { name: "a.mp4", size: 1000, mime: "video/mp4" }, delivery: { state: "ready", saved_at: null }, actions: { pause: false, resume: false, cancel: false, retry: false, delete: true, download: true } })} /></ul>);
    await user.click(screen.getByRole("button", { name: /Download to this device/ }));
    await waitFor(() => expect(clicks).toEqual(["/api/downloads/tok_abc"]));
    expect(calls[0]!.headers.get("X-CSRF-Token")).toBe("csrf");
  });
});

describe("history", () => {
  it("paginates", async () => {
    const { calls } = mockFetch({
      "GET /api/jobs": ({ url }) => url.searchParams.get("page") === "2"
        ? { body: page([job({ id: "b", title: "Older item", status: "canceled", actions: { pause: false, resume: false, cancel: false, retry: true, delete: true, download: false } })], { total: 12, page: 2, page_size: 10 }) }
        : { body: page([job({ id: "a", title: "Newest item", status: "completed" })], { total: 12, page: 1, page_size: 10 }) },
    });
    const user = userEvent.setup();
    renderWithClient(<HistoryScreen online />);
    expect(await screen.findByText("Newest item")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Older/ }));
    expect(await screen.findByText("Older item")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/jobs")).toBe(true);
  });
  it("shows an empty state", async () => {
    mockFetch({ "GET /api/jobs": () => ({ body: page([]) }) });
    renderWithClient(<HistoryScreen online />);
    expect(await screen.findByText("No history yet")).toBeInTheDocument();
  });
  it("shows an error with a retry action", async () => {
    mockFetch({ "GET /api/jobs": () => ({ status: 500, body: { error: { code: "internal_error", message: "Something went wrong." } } }) });
    renderWithClient(<HistoryScreen online />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong.");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("polling policy", () => {
  it("backs off exponentially up to 30s while failing", () => {
    expect([0, 1, 2, 3, 4, 5, 9].map((n) => backoffInterval(2000, n))).toEqual([2000, 4000, 8000, 16000, 30000, 30000, 30000]);
  });
  it("retries only transient errors", () => {
    expect(shouldRetry(0, new NetworkError())).toBe(true);
    expect(shouldRetry(0, new ApiError(503, "x", "x"))).toBe(true);
    expect(shouldRetry(0, new ApiError(422, "x", "x"))).toBe(false);
    expect(shouldRetry(0, new ApiError(401, "x", "x"))).toBe(false);
    expect(shouldRetry(2, new NetworkError())).toBe(false);
  });
});
