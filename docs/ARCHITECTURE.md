# Architecture

```
 Browser (React SPA)  ─┐                     ┌─ PostgreSQL  (source of truth)
 Android (Capacitor) ──┼─► Nginx ─► FastAPI ─┤
                       │   (TLS)    (API)    └─ Redis (Celery broker + rate limits)
                       │                          ▲
                       │            Celery worker ┘ ──► egress proxy ──► internet
                       │            (yt-dlp/aria2c/ffmpeg)   (SSRF boundary)
                       └─ private media volume ◄── worker writes, API streams (read-only)
   scheduler (celery beat + maintenance worker): lease recovery, redispatch, retention cleanup
```

| Path | Responsibility |
| --- | --- |
| `backend/app/api/` | Routers: `auth`, `jobs` (+inspections, usage), `files` (delivery, tickets). Thin; logic is in services |
| `backend/app/schemas.py` | Public response shapes. Deliberately has no path, command, lease or log fields |
| `backend/app/models.py`, `alembic/` | SQLAlchemy models and migrations |
| `backend/app/states.py` | The job state machine (single definition) |
| `backend/app/services/jobs.py` | Job creation, user controls, worker claim/lease/complete/fail (all transitions) |
| `backend/app/services/engine.py` | Safe yt-dlp command construction, progress parsing, error classification, inspection parsing |
| `backend/app/services/runner.py` | Subprocess under a pty, control polling, process-group termination |
| `backend/app/services/storage.py` | Server-generated paths, symlink-safe open, quotas, filenames |
| `backend/app/services/urlpolicy.py` | URL syntax policy + resolved-address policy |
| `backend/app/services/reconcile.py` | Lease recovery, redispatch, retention cleanup (idempotent, advisory-locked) |
| `backend/app/worker/` | Celery app, tasks, `executor.py` (one download attempt) |
| `backend/app/egress_proxy.py` | SSRF-enforcing forward proxy |
| `backend/app/cli.py` | `create-user`, `import-legacy`, `stats`, `reconcile`, `cleanup`, `set-quota` |
| `frontend/` | React + TypeScript + Vite + Tailwind; Capacitor wrapper in `frontend/android` |
| `ytaria.py` | Legacy single-user local tool (see `LEGACY.md`) |

## Product flow

1. `POST /api/inspections {url}` – URL policy check, then a Celery **inspect** task runs `yt-dlp --dump-single-json` on the
   worker (metadata extraction is network activity, so it goes through the same egress boundary, timeout and rate limit).
   The client polls `GET /api/inspections/{id}`. The result is sanitised and reduced to a **fixed vocabulary of quality
   options** (`best`, `h1080`, `audio_mp3`, …).
2. `POST /api/jobs {inspection_id, selection}` + `Idempotency-Key`. The selection must be one of the options of that
   inspection; the server maps it to a yt-dlp format string. Clients never supply yt-dlp arguments.
3. A worker claims the job, downloads to a per-job work directory, moves the finished file to `final/media.<ext>`.
4. `GET /api/jobs/{id}/file` (auth) or a short-lived ticket (`POST …/download-ticket` → `GET /api/downloads/{token}`) streams the
   file with Range support. The client reports `saved`/`failed` via `POST …/transfer`.

**"Ready on server" ≠ "Saved on device".** Job `status=completed` is server processing. `delivery.state` is separate:
`unavailable → ready → transferring → sent → saved`. `sent` means the server streamed every byte; `saved` exists only
because the client said so (the server cannot verify it), and the web UI never claims more than "Sent to browser".

## Job state machine

```
            ┌────────── resume ───────────┐
            ▼                             │
 queued ──claim──► running ──ok──► completed ──retention──► expired ──retry──► queued
   │  ▲               │ │ └──fail (retryable, attempts left, backoff)──► queued
   │  └── lease lost ─┘ │ └──fail (permanent / attempts exhausted / timeout)──► failed ──retry──► queued
   │                    ├─pause req─► pausing ──worker stops──► paused ──cancel──► canceled ──retry──► queued
   │                    └─cancel req► canceling ─worker stops─► canceled
   ├─pause (no process yet)──► paused ──► (resume ► queued | cancel ► canceled | age out ► expired)
   └─cancel (no process yet)─► canceled
```

* Defined once in `states.py`; every change goes through `jobs.move()` under a row lock and writes a `job_events` row.
  A test asserts every undeclared transition is illegal.
* `pausing`/`canceling` are the honest intermediate states: the API cannot signal a process in another container, so it
  records the request and the worker (which heartbeats every few seconds) observes it and stops the real process group.
* `canceling → completed` is deliberately impossible: a cancel always wins a race with a finishing process.
  `pausing → completed` is allowed (the work is done; keep it).
* Control endpoints are idempotent: pausing a paused job, cancelling a cancelled job, resuming a queued job all return 200.

## Reliability

* **Durability**: the job row is committed before anything is published. If the broker is down the job stays `queued` with
  `enqueued_at = NULL`; the reconciler (every 15 s) publishes it. Redis loss cannot lose or alter user-visible state.
* **Claiming**: `claim_job` takes a transaction-scoped advisory lock, checks global and per-user concurrency and disk headroom,
  then flips `queued → running` with a fresh `lease_token`. A duplicate delivery finds the job not `queued` and exits.
* **Leases & fencing**: workers renew `lease_expires_at` on each heartbeat. Every worker write (progress, complete, fail) is
  conditional on `lease_token`, so a worker that lost its lease (crash recovery already re-queued the job) cannot overwrite
  newer state. Verified with a real `docker kill` of the worker container mid-download (job recovered and completed on attempt 2).
* **Retries**: bounded (`YTARIA_MAX_ATTEMPTS`, default 3) with exponential backoff + jitter. Only failures classified as
  transient (network, 5xx, source rate limiting) are retried; private/unavailable/unsupported/too-large fail immediately.
  yt-dlp's own retries are finite (5). The legacy `--retries infinite` is gone.
* **Limits**: max runtime (`JOB_MAX_RUNTIME_SECONDS`, default 2 h), max file size, per-user storage quota, min free disk,
  global concurrency (2), per-user concurrency (1), per-user active jobs (5), per-user rate limits.
* **Termination**: the runner starts yt-dlp in its own session/process group and always terminates the *whole group*
  (SIGTERM → grace → SIGKILL), so aria2c/ffmpeg children die too. `setpriv --pdeathsig KILL` covers a SIGKILLed worker.

### Pause/resume honesty
Pause stops the process and keeps the work directory. Resume starts yt-dlp again on the same directory; yt-dlp/aria2c reuse
`.part`/`.aria2` files **when the source supports range requests**. Some sources/formats (HLS fragments, servers without
ranges, merged formats) restart or re-fetch parts; byte-perfect resume is not promised. A pause does not consume the retry budget.

### Cleanup policy
| Situation | Files |
| --- | --- |
| paused | kept; expire after `PAUSED_MAX_AGE_HOURS` (72) |
| canceled | deleted immediately by the worker (or by the next sweep if canceled before it started) |
| failed | permanent failures free partials immediately; transient ones keep them for `FAILED_PARTIAL_RETENTION_HOURS` (24) so *Retry* can reuse them |
| completed | deleted after `COMPLETED_RETENTION_HOURS` (168) → status `expired` |
| user-removed | deleted by the next sweep |
Sweeps skip jobs with an active transfer; a stream that already opened its file keeps reading after the unlink (tested).

## Authentication

Two clients, one server, one session model (a *family* of rotating refresh tokens):

| | Browser | Android (Capacitor) |
| --- | --- | --- |
| Access credential | 15-min JWT in an `HttpOnly; SameSite=Strict; Secure` cookie (`Path=/api`) | same JWT, in memory, sent as `Authorization: Bearer` |
| Refresh credential | opaque token in `HttpOnly` cookie (`Path=/api/auth`) | opaque token in Android Keystore-backed storage (`@aparajita/capacitor-secure-storage`, AES-GCM) |
| CSRF | cookie-authorised unsafe requests need `X-CSRF-Token` = HMAC(secret, session id) **and** a matching `Origin`; refresh needs `X-Requested-With` + `Origin` | not applicable (no ambient credentials) |
| Selected by | default | request header `X-Client: native` (tokens then travel in the body, never as cookies) |

* Passwords: argon2id. Constant-work login for unknown accounts. Generic error for wrong email/password.
* Refresh tokens are stored hashed, rotated on every use; replaying a rotated token after a 10 s grace (multi-tab race) revokes the
  whole family. Logout revokes the family, and access tokens of a revoked family stop working immediately (checked per request).
* Nothing sensitive goes into `localStorage`/`sessionStorage` (a test asserts it). Only the theme and the unsent URL draft do.
* Rate limits (Redis, fail closed): login per IP and per account, register, refresh, inspect, job creation, tickets. Nginx adds
  per-IP request/connection limits in front.
* Not implemented (documented limitation): e-mail verification and password reset. Use `registration_mode=invite`/`closed`
  and `python -m app.cli create-user` for controlled deployments.

## Data model
`users` · `refresh_tokens` (family, hash, rotation) · `inspections` · `jobs` (state, lease, progress, server-side paths/log tail)
· `job_events` (user-visible timeline) · `download_tickets` (hashed) · `transfers` (server→device tracking) · `legacy_imports`.
Database-level guards: partial unique index `(user, url_hash, selection)` over *active* jobs (duplicate submission),
unique `(user, idempotency_key)`, status CHECK constraint.
