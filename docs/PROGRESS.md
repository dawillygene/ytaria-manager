# Progress log (hand-off document)

Status of the plan from the original brief. "Verified" = executed in this repository's development environment; see the evidence list for exact commands.

| # | Phase | Status |
| --- | --- | --- |
| 1 | Repository review, plan, baseline | Done – `BASELINE.md` |
| 2 | Backend structure, DB, auth | Done – FastAPI, SQLAlchemy 2, Alembic `0001`, argon2id, JWT + rotating refresh, CSRF, rate limits |
| 3 | Worker + secure job lifecycle | Done – Celery/Redis, leases/fencing, reconciler, bounded retries, group kill, limits |
| 4 | Private storage + delivery | Done – symlink-safe storage, Range streaming, tickets, retention/cleanup, quotas |
| 5 | React web UI | Done – auth, inspect, quality, queue, controls, ready/saved, history+pagination, settings, offline states |
| 6 | Capacitor Android | Done – debug + release APK built; run on an emulator; foreground transfer only |
| 7 | Deployment + docs | Done – compose (dev/prod), Nginx TLS, certbot script, backup/restore, egress boundary verified |
| 8 | Integration tests, security checks, final verification | Done for what the environment allows – see "Not verified" |

## Evidence (all run on 2026-09-19 in the dev environment)
* Backend: `cd backend && ../.venv/bin/python -m pytest` → **189 passed** (PostgreSQL 17 + Redis 7 in Docker; migrations applied from scratch by the suite).
* Frontend: `cd frontend && npm run typecheck && npm test` → **35 passed**; `npm run build` OK; `npm audit --omit=dev` → 0 vulnerabilities.
* Real-stack E2E (`backend/scripts/smoke_e2e.py`, real yt-dlp + aria2c + ffmpeg + Celery, public-domain archive.org video): passed both against local processes and against the Docker Compose stack
  (register ×2, inspect, idempotent submit, pause@13–42 %, resume, complete 26,138,707 B, cross-user 404s, ranged + full ticket transfer with matching SHA-256, saved report, cancel).
* Real browser (headless Chrome via Playwright, iPhone-width viewport, Vite dev server → API): register → HttpOnly cookies, no tokens in web storage → inspect → server download → browser download (exact byte count) → history/settings → reload keeps session → logout → API returns 401. Screenshots in `docs/screenshots/`.
* Worker crash recovery: `docker kill` of the worker container mid-download → lease expired (~62 s) → reconciler re-queued → attempt 2 completed.
* Egress boundary (from inside the worker container of the compose stack): see `SECURITY.md`.
* Log redaction: Nginx and API logs contain no download tickets (`GET /api/downloads/[redacted]`).
* HTTPS mode: Nginx with a self-signed cert → HTTP/2, HSTS, HTTP→HTTPS redirect (Let's Encrypt itself not run: needs a real domain).
* Backup/restore: `deploy/backup.sh` + `deploy/restore.sh` round trip on the running stack.
* Android: debug + release(R8) APKs built with Gradle 8.14/JDK 21; run on an API-35 emulator (details in `ANDROID.md`).

## Not verified / open items
* **Public deployment safety is not claimed.** Nothing was deployed. The egress boundary was verified on one Docker host only. No third-party security review. Load/capacity was not measured.
* Let's Encrypt issuance/renewal, GitHub Actions run (the workflow was written but not executed), Play Store/signing, physical Android devices, iOS.
* No e-mail verification, password reset, MFA, admin UI (operators use the CLI). No background/durable device transfer. No object-storage backend (interface points identified in `DEPLOYMENT.md`).
* Supported sites are an allow-list (`youtube.com, youtu.be, vimeo.com, dailymotion.com, archive.org`). Only archive.org was exercised against the network here; YouTube frequently requires sign-in/bot checks from server IPs, and those downloads fail cleanly ("requires an account or is restricted") because cookie use was deliberately removed.
* The repository has **no commits** for this work: everything is left in the working tree for you to review (`git status`).

## Where to continue
1. Review `git status`/diff, then commit in logical pieces (backend, frontend, android, deploy, docs).
2. Provide: production domain, DNS, VPS, `.env` secrets, Android signing keystore, final icons/splash.
3. Run the checklist in `SECURITY.md` → "Before going public" on the real host; run `backend/scripts/smoke_e2e.py` against it.
4. Candidate follow-ups (in priority order): password reset + e-mail verification; per-user admin/abuse tooling; durable Android background transfer; S3-compatible storage; metrics endpoint (Prometheus); on-device Range resume.
