# Baseline review of the legacy `ytaria.py` (Phase 1)

Reviewed at commit `32d30fd`. Everything below was verified by reading
`ytaria.py` (1480 lines) and by running `python3 ytaria.py add/list` against a scratch
database. There were no tests, no lockfile and no CI.

## What exists

| Area | Legacy behaviour |
| --- | --- |
| Queue | SQLite `jobs` table, integer ids, states `pending/running/done/failed/canceled/paused` |
| Worker | One thread inside the web process, guarded by an `flock` file. Claims with `BEGIN IMMEDIATE` |
| Engine | `yt-dlp -f "bv*+ba/b" --downloader aria2c --merge-output-format mp4` run under a pty so aria2c prints progress; progress/speed/ETA scraped with regexes |
| Controls | pause = SIGTERM the process group (aria2c `.aria2` file lets it resume), resume, cancel, retry |
| Frontends | `ThreadingHTTPServer` + embedded Tailwind-CDN page; curses TUI; argparse CLI (`add`, `list`, `serve`, `start/stop/status/restart`, `worker`, `tui`) |
| Storage | Files land in `~/Downloads/ytaria-downloads`; nothing is ever served to a client |

## Behaviour that must be preserved

* Best-quality default selector `bv*+ba/b` (the `/b` fallback matters for progressive-only videos).
* yt-dlp + aria2c + ffmpeg, MP4 merge, `--no-playlist`, `--restrict-filenames`.
* Running the process in its own session and signalling the **whole group** (yt-dlp spawns aria2c).
* Pty-based progress capture, `\r` treated as a line break, ANSI stripped, last ~20 lines kept for diagnostics.
* Pause keeps partial data; resume continues from it where the source supports ranges.
* CLI/TUI surface (kept working as *legacy local mode*, see `docs/LEGACY.md`).

## Concrete risks found

Severity is for a multi-user, internet-facing deployment (the target), not for the original single-user localhost tool.

1. **Command/option injection (critical)** – the URL is appended as the last yt-dlp argument with no `--` separator
   (`build_yt_dlp_command`, l.380-405). `url="--exec=..."` becomes a yt-dlp option. No scheme validation at all.
2. **Arbitrary output directory (critical)** – `_create_job` takes `output_dir` from the JSON body
   (l.760) and passes it to `mkdir(parents=True)` and `yt-dlp -P`; any writable path can be targeted.
3. **No authentication or user isolation (critical)** – every endpoint is anonymous; jobs have sequential integer ids
   (trivially enumerable) and every job is returned to every caller.
4. **No SSRF controls (critical)** – any host/scheme yt-dlp understands is fetched, including
   `localhost`, RFC1918 and `169.254.169.254`; redirects/manifests/fragments are unrestricted.
5. **Sensitive data in API responses (high)** – `SELECT *` is returned: absolute filesystem paths (`output_dir`,
   `destination`), the full command line, `cookies_browser`, and raw yt-dlp/aria2c log tails.
6. **Browser-cookie extraction (high)** – `--cookies-from-browser`/`YTARIA_COOKIES_*` read the *server user's*
   browser profile and reuse one personal account for every job. Not acceptable for a shared service.
7. **Infinite retries (high)** – `--retries infinite --fragment-retries infinite`; a dead source never fails.
8. **No timeouts / size limits (high)** – no maximum runtime, no `--max-filesize`, no quota, no disk-space check.
9. **Unbounded request handling (medium)** – `Content-Length` is trusted and read fully; `ThreadingHTTPServer`
   spawns unlimited threads; `json.loads` errors return HTML 500s.
10. **Inherited unsafe configuration (medium)** – yt-dlp is run without `--ignore-config`, so `~/.config/yt-dlp`,
    plugins and system config influence behaviour; the environment is inherited wholesale.
11. **Crash recovery discards work (medium)** – `requeue_orphans` blindly resets *every* `running` job to `pending`
    with `progress=0` at start-up; there are no leases, so a second process cannot tell a live job from a dead one.
12. **Control races (medium)** – `Worker.current` is read/written from HTTP threads and the worker thread without a
    lock; `retry` mutates `cookies_browser`/`command` *before* checking the job is retryable; a cancel that lands just
    before exit can still be recorded as `done`/`failed` depending on which branch wins.
13. **Single-process coupling (medium)** – API and worker share a process, so scaling or restarting one restarts the
    other, and `flock` is the only guard against duplicate workers (local-filesystem only).
14. **UI (low)** – Tailwind from a CDN (supply-chain + offline), inline `onclick` handlers, no CSP.
15. **Repo hygiene (low)** – `__pycache__` directories sit in the working tree (git-ignored, but noisy), the README hard-codes `/home/...` paths, and there is no lockfile, CI or test.

## Decisions taken in response

* New multi-user product lives in `backend/` (FastAPI + Celery + PostgreSQL) and `frontend/` (React + Capacitor).
* `ytaria.py` stays as **legacy local mode** with the unsafe features removed (cookies, infinite retries, arbitrary
  `output_dir`, option injection). It never talks to the new queue. See `docs/LEGACY.md`.
* One optional, explicit SQLite → PostgreSQL import command instead of any automatic migration.
