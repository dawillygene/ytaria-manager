# Deployment (single Linux VPS, Docker Compose)

Nothing here is executed for you. **Do not deploy publicly until you have read `SECURITY.md` ("Before going public").**
Prerequisites: a Linux VPS with Docker Engine 26+ and the Compose plugin, a DNS name pointing at it, ports 80/443 open
(and SSH), and roughly the disk you want to offer users (see "Sizing"). No hosting is recommended or priced here.

## 1. Configure
```bash
git clone <your repo> ytaria && cd ytaria
cp .env.example .env && chmod 600 .env
# fill YTARIA_DOMAIN, YTARIA_SECRET_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD (openssl rand -hex 32), registration mode/code
```
`YTARIA_ENV=production` is the default in `docker-compose.yml`; the API refuses to start without a strong secret, secure cookies and
the egress proxy. Only the `web` service publishes ports (80, 443). PostgreSQL and Redis have no published ports.

## 2. Build, migrate, start
```bash
docker compose build
docker compose up -d db redis
docker compose run --rm migrate          # alembic upgrade head (also runs automatically before api/worker start)
docker compose up -d
docker compose ps                        # all "healthy" after ~1 minute
```
Startup order is enforced: `db`/`redis` healthy → `migrate` completed → `api`/`worker`/`scheduler` → `web`.

## 3. HTTPS (Let's Encrypt, HTTP-01)
```bash
./deploy/certbot.sh init downloads.example.com you@example.com
```
This creates a temporary self-signed certificate so Nginx can start, obtains the real certificate through the ACME webroot, and
publishes it to `data/certs/` owned by the Nginx user (Let's Encrypt keys are root-only; Nginx runs as uid 101), then reloads Nginx.
Renewal (twice daily is the certbot recommendation):
```cron
17 3,15 * * * cd /opt/ytaria && ./deploy/certbot.sh renew >> /var/log/ytaria-certbot.log 2>&1
```
Nginx serves HTTP/2, TLS 1.2/1.3, HSTS, redirects port 80 → 443. Verified locally with a self-signed certificate (HTTP/2, HSTS, redirect);
the Let's Encrypt issuance itself needs your real domain and was **not** exercised.
Already terminating TLS elsewhere (a cloud load balancer)? Run the `YTARIA_TLS=off` variant behind it and set `YTARIA_COOKIE_SECURE=true`.

## 4. First user and checks
```bash
docker compose exec api python -m app.cli create-user you@example.com          # prompts for a password
curl -fsS https://downloads.example.com/api/ready                              # {"status":"ok","database":"ok","redis":"ok"}
docker cp deploy/egress_probe.py "$(docker compose ps -q worker)":/tmp/ && docker compose exec worker python /tmp/egress_probe.py
```
The probe must show direct connections **blocked**, `api`/`web` unreachable, and every internal/metadata target answered with `403 blocked`.
Optional end-to-end check with media you are authorised to download:
`python backend/scripts/smoke_e2e.py --base-url https://downloads.example.com --url <public-domain video URL>` (registration must be open).

## 5. Operate
| Task | Command |
| --- | --- |
| Logs | `docker compose logs -f api worker scheduler egress web` (JSON; tokens/tickets redacted; json-file rotation 5×10 MB) |
| Queue/disk stats | `docker compose exec api python -m app.cli stats` |
| Force a sweep | `docker compose exec scheduler python -m app.cli reconcile` / `cleanup` (cleanup needs the writable media mount, which the API does not have) |
| Raise a user's quota | `docker compose exec api python -m app.cli set-quota user@example.com --gib 50` |
| Graceful stop | `docker compose stop` (api 30 s, worker 60 s; running downloads are recovered via lease expiry and resume from partial data) |

Exactly one `scheduler` container must run (celery beat + maintenance worker); do not scale it. The reconcile/cleanup tasks
additionally take a PostgreSQL advisory lock, so an accidental second scheduler cannot double-act.

## 6. Backup and restore
```bash
./deploy/backup.sh                    # backups/db-<timestamp>.dump  (custom-format pg_dump; mode 0600)
WITH_MEDIA=1 ./deploy/backup.sh       # also tars the media volume (usually unnecessary: files expire; can be large)
./deploy/restore.sh backups/db-<timestamp>.dump   # DESTRUCTIVE: asks you to type "restore"
```
`restore.sh` stops api/worker/scheduler/web, restores with `pg_restore --clean`, runs migrations, restarts. Tested end to end (delete all rows →
restore → 3 users / 3 jobs back). Copy backups off the host; test restores periodically. Redis holds only in-flight broker messages and rate-limit counters (AOF enabled).
Secrets live in `.env`: back it up separately and securely.

## 7. Upgrade and rollback
```bash
./deploy/backup.sh
docker tag ytaria-backend:latest ytaria-backend:prev && docker tag ytaria-web:latest ytaria-web:prev
git pull && docker compose build && docker compose up -d       # migrations run before the new api/worker start
```
Rollback code: `git checkout <previous>`; `docker tag ytaria-backend:prev ytaria-backend:latest` (and web) and `docker compose up -d --no-build`.
Rollback schema only if the release added a migration: `docker compose run --rm api alembic downgrade -1` (review the migration first) or restore the
pre-upgrade dump. Migrations are additive by default; never run destructive ones without a fresh backup.

## 8. Sizing: measure, don't guess
No capacity or price claims are made. Measure your own workload with a realistic user count and the sources you allow:
* **CPU/memory per container**: `docker stats` while N downloads run; ffmpeg merging is the CPU-heavy phase. Tune `WORKER_CPUS`, `WORKER_MEM_LIMIT`, `--concurrency`, `YTARIA_MAX_CONCURRENT_JOBS_GLOBAL`.
* **Disk**: `df -h` / `docker system df`, and `python -m app.cli stats` (`disk_bytes_tracked`). Peak use ≈ concurrent jobs × up to 2× file size (separate streams + merged output) + retained files (users × quota is the worst case). Retention (`YTARIA_COMPLETED_RETENTION_HOURS`) and quotas are your levers.
* **Outbound bandwidth** (download from sources) and **inbound-to-users** (file transfers): `vnstat -i <iface>` or your provider's meter; each completed file is downloaded once from the source and transferred once per save.
* **API/DB**: `docker compose exec db psql -U ytaria -c "select status,count(*) from jobs group by 1"`; slow queries via `pg_stat_statements` if needed.
Pick the plan from those numbers plus headroom, then re-measure after launch.

## 9. Scaling later
* **Separate workers**: the worker only needs PostgreSQL, Redis and the egress proxy. Move `worker` (+ its `egress`) to another host with private connectivity to db/redis; it must still share the media directory with the API → use NFS/shared volume, or the object-storage step below.
* **Object storage**: file access is isolated in `services/storage.py` (`open_stored_file`, `finalize_file`, `remove_*`). Replace it with an S3-compatible backend and serve via short-lived presigned URLs (the ticket endpoint already models this); keep local disk only for work directories.
* More API capacity: raise uvicorn `--workers`, or run more `api` replicas behind Nginx (`proxy_pass` to a service name).
