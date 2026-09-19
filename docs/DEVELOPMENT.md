# Development

## Prerequisites
Python 3.12, Node 22, Docker (for PostgreSQL/Redis), `yt-dlp`, `aria2c`, `ffmpeg` on PATH (only needed for real downloads; the test-suite uses a fake yt-dlp).

## Services
```bash
docker run -d --name ytaria-dev-pg -e POSTGRES_USER=ytaria -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=ytaria -p 127.0.0.1:55432:5432 postgres:17-alpine
docker run -d --name ytaria-dev-redis -p 127.0.0.1:56379:6379 redis:7-alpine
```
These match the defaults in `backend/app/config.py` and CI. All settings are `YTARIA_*` environment variables (see `config.py`).

## Backend
```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cd backend
../.venv/bin/alembic upgrade head
YTARIA_ALLOW_DIRECT_EGRESS=true ../.venv/bin/uvicorn app.main:app --reload            # http://127.0.0.1:8000/api/docs
../.venv/bin/celery -A app.worker.celery_app worker -Q downloads,inspect,maintenance -c 3
../.venv/bin/celery -A app.worker.celery_app beat                                   # exactly one
../.venv/bin/python -m pytest                                                        # needs the two containers above
../.venv/bin/python scripts/smoke_e2e.py --base-url http://127.0.0.1:8000            # real yt-dlp, public-domain video
```
Development uses direct egress (`YTARIA_ALLOW_DIRECT_EGRESS=true`, refused in production). To exercise the real boundary use the compose dev stack:
`docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file <env> up --build` → http://localhost:8080.

## Frontend
```bash
cd frontend && npm ci
npm run dev            # http://127.0.0.1:5173, proxies /api to 127.0.0.1:8000 (same-origin: cookie auth works as in production)
npm run typecheck && npm test && npm run build
```

## Updating dependencies
Edit `backend/requirements.in` / `requirements-dev.in`, run `backend/scripts/lock.sh` (rebuilds `requirements*.txt` from clean virtualenvs), commit both.
Frontend: `npm install <pkg>` updates `package-lock.json`. Versions in use are current as of 2026-09 (FastAPI 0.141, SQLAlchemy 2.0.54, Celery 5.6, React 19.3, Vite 8.3, Tailwind 4.3, Capacitor 8.5).

## Tests (what covers what)
`backend/tests`: `test_auth` (register/login/logout/refresh rotation+reuse/expiry/CSRF/CORS-origin/rate limit), `test_isolation` (cross-user), `test_urlpolicy`+`test_egress_proxy` (SSRF),
`test_storage` (traversal/symlinks/filenames), `test_jobs` (dedupe, idempotency, limits, state machine, claim races, cancel-vs-complete, crash recovery, backoff),
`test_executor` (real subprocess groups: cancel kills grandchildren, pause keeps partials, retries, timeouts, quota, lease loss), `test_files` (Range/If-Range/HEAD/tickets/expiry),
`test_reconcile` (retention, active transfer protection, orphans, advisory locks), `test_legacy_import`, `test_legacy_script`, `test_engine`, `test_api_security`.
`frontend/src`: API client/session (refresh single-flight, offline ≠ logout, no credentials in web storage, native secure-storage flow), auth form, submit flow (idempotency key, double-submit), job card (XSS as text, actions from server flags, confirmations, ready≠saved), history pagination, polling backoff.
