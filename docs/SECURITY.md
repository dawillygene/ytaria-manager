# Security model and controls

> **Public-deployment status: NOT declared safe.** The controls below are implemented and tested, but the SSRF boundary
> depends on the Docker network topology in `docker-compose.yml`. It was verified on a single Docker host (results below);
> it has **not** been verified on your VPS, and a third-party review has not happened. See "Before going public".

## Threat model (short)
Untrusted, authenticated users submit URLs to a service that fetches them from inside your network. The main risks are
SSRF (reaching internal services/cloud metadata), command/option injection, cross-user data access, path traversal on
stored files, resource exhaustion (disk, bandwidth, CPU, concurrency) and credential theft.

## SSRF (metadata extraction, downloads, redirects, manifests, fragments)
Layered, because checking the first URL is not enough — yt-dlp, aria2c and ffmpeg make their own requests:

1. **API URL policy** (`urlpolicy.validate_source_url`): http/https only; no embedded credentials; ports 80/443 only; no IP
   literals, no numeric/hex/octal host tricks, no `localhost`/`.internal`/`.local`; host must match the **source allow-list**
   (`YTARIA_ALLOWED_SOURCE_HOSTS`, conservative default). Re-checked by the worker before it runs anything.
2. **yt-dlp is restricted**: `--use-extractors default,-generic` (the generic extractor would follow arbitrary pages),
   `--ignore-config --no-plugin-dirs`, `--` before the URL, no client-supplied arguments, no `--exec`, scrubbed environment
   (no application secrets), argument arrays only.
3. **Egress proxy = enforcement point** (`app/egress_proxy.py`): all worker traffic goes through it (`--proxy`, aria2c
   `--all-proxy`, `http(s)_proxy` env). It resolves DNS itself, requires *every* answer to be a public unicast address
   (IPv4 and IPv6, incl. v4-mapped, 6to4, NAT64, Teredo, CGNAT, link-local/metadata, ULA, multicast, reserved), allows only
   ports 80/443, and connects to the **validated IP** (no second resolution → DNS rebinding cannot flip the answer).
   Redirects/manifests/fragments are new requests through the same proxy, so each hop is re-checked.
4. **Network isolation**: the worker container is on `internal: true` networks with **no route to the internet and no DNS**.
   A tool that ignores the proxy simply cannot connect. Only the `egress` container has outbound access.
   The worker also cannot reach `api` or `web` (separate networks); it can reach `db`/`redis`, which it needs.
5. **Production guard**: with `YTARIA_ENV=production` the API refuses to start without `YTARIA_EGRESS_PROXY_URL`; the worker
   fails jobs with `egress_unconfigured` rather than downloading with direct egress.

**Verified** (Docker 26, this repo's compose, from inside the worker container): direct TCP to `1.1.1.1:443` and
`169.254.169.254:80` fail; `getaddrinfo(archive.org)` fails; `api`, `web` unreachable; via proxy: `169.254.169.254`, `127.0.0.1`,
`db:5432`, `api:8000`, `10.0.0.1:443`, `[::1]` → 403 `X-Ytaria-Egress: blocked`; `https://archive.org` → 200. Unit tests cover the
IP classifier (26 cases), rebinding-style mixed answers and CONNECT/plain-HTTP forwarding over real sockets.

**Residual risks / requirements on your deployment**
* Keep the network layout. Do not put `worker` on a non-internal network or publish db/redis ports.
* The `egress` container can reach whatever your host network can reach *if the destination is public*. If your VPS provider
  exposes internal services on public-looking addresses, add host firewall rules.
* Plain-HTTP requests through the proxy are supported; HTTPS is tunnelled (CONNECT) so the proxy validates host:port, not paths.
* Allow-listing means new sites need a config change; that is intentional for the first release.

## Authorisation and isolation
Every job/inspection/event/file query is filtered by the authenticated user id; other users' ids return an identical `404`
(tested against 10 endpoints). Identifiers are random UUIDs. Download tickets are bound to one job + user, expire in 120 s,
are stored hashed, and never logged (Nginx redacts `/api/downloads/*`; the app logs route templates). API responses contain no
paths, commands, tokens, leases or worker logs (tests assert absence). Raw tool output is stored only in `jobs.debug_tail`
(redacted) and mapped to a fixed set of public error messages.

## Files
Server-generated paths only (`jobs/<uuid>/final/media.<ext>`), never client input. `resolve_inside` rejects absolute/`..`
paths and any symlink component; files are opened with `O_NOFOLLOW` and `fstat` must show a regular file. Tests cover
symlink swap, traversal values in the DB, and outside-root deletion attempts. Downloaded names are sanitised; responses are
`Content-Disposition: attachment` with an extension→type allow-list and `nosniff`. Directories are `0700`, files `0600`.
Streaming (1 MiB chunks) with `Range`/`If-Range`/`HEAD`; nothing is loaded into memory.

## Web hardening
Body limit 64 KiB (also chunked), explicit CORS origins (`https://localhost` for the Capacitor WebView), `nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, strict CSP (no inline scripts; Tailwind ships as a file), HSTS in
TLS mode, `Cache-Control: no-store` on API responses. React renders untrusted titles/errors as text (tests with `<script>`/`<img onerror>`).

## Containers
Non-root (uid 10001 / nginx 101), `cap_drop: ALL`, `no-new-privileges`, read-only root filesystems (API/worker/scheduler/proxy) with
tmpfs, CPU/memory/pids limits on worker, json-file log rotation, secrets only via environment (`.env`, git-ignored). Redis requires a password.

## Removed on purpose
Browser-cookie extraction (`--cookies-from-browser`, `cookies.txt`) is gone from the product and the legacy tool. There is no DRM
handling or access-control circumvention; restricted content simply fails with "requires an account or is restricted".

## Known limitations
* No e-mail verification / password reset / MFA.
* Registration `open` is an abuse risk on a bandwidth-heavy service; default in the compose file is `invite`.
* Client-reported `saved` is informational.
* `npm audit` (dev-only) reports advisories inside `@capacitor/cli`'s toolchain (tar/uuid); runtime dependencies audit clean.
* Worker crash while a job runs: `pdeathsig` kills yt-dlp, but an aria2c child in a SIGKILLed container is only cleaned by the container stopping.
* No antivirus/content scanning of downloaded files (they are served as attachments only).

## Before going public
Confirm `docker exec <worker> python /tmp/egress_probe.py`-style checks on your host (script in `docs/DEPLOYMENT.md`), set
`YTARIA_REGISTRATION_MODE`, put the host firewall in front (only 80/443), enable unattended OS updates, monitor disk, and consider an
independent review. Legal responsibility for what users download stays with the operator.
