# Legacy local mode (`ytaria.py`) and migration

`ytaria.py` is the original single-user tool (SQLite queue, built-in HTTP server, curses TUI, CLI). It still works, **separately** from the new product:
it uses its own SQLite database (`~/.local/share/ytaria-manager/jobs.sqlite3`) and its own in-process worker. It never touches PostgreSQL/Celery, so the old and new
workers can never consume the same queue. Use it only on your own machine (it binds to 127.0.0.1 and has no authentication).

## What changed (behaviour differences)
| Before | Now |
| --- | --- |
| `--cookies-from-browser`, `YTARIA_COOKIES_*`, browser menu in the web UI | **Removed.** The tool never reads browser profiles or cookie files |
| URL appended without `--`; any scheme accepted | `validate_url` (http/https only, no credentials, no whitespace) and `--` before the URL |
| `--retries infinite` / `--fragment-retries infinite` | 5 / 5 (aria2c `--max-tries=5`) |
| Web API accepted a client `output_dir` | Ignored: always `~/Downloads/ytaria-downloads` (CLI `add --output-dir` still works: it is your own shell) |
| API returned the command line and cookie column | Stripped from responses |
| yt-dlp inherited user/system config | `--ignore-config` |
| POST bodies unbounded | 64 KiB limit, JSON errors → 400 |
CLI/TUI/`start|stop|status|restart|serve|worker|tui|add|list` commands are unchanged except `add --cookies-from-browser`, which no longer exists.
Existing databases keep working (the old `cookies_browser` column is left in place, never read).
For anything shared, internet-facing, multi-user or mobile, use the new stack.

## Optional import into the new product
Explicit, never automatic. The new server must be set up and the owner account must exist.
```bash
python -m app.cli create-user me@example.com                      # in Docker: docker compose exec api python -m app.cli create-user …
python -m app.cli import-legacy --sqlite ~/.local/share/ytaria-manager/jobs.sqlite3 \
    --owner me@example.com --files-root ~/Downloads/ytaria-downloads --dry-run
python -m app.cli import-legacy … (same arguments without --dry-run)
```
Behaviour (all covered by tests): the source is opened **read-only** and backed up first (`jobs.sqlite3.bak-<timestamp>`, mode 0600; skipped for `--dry-run`); `--owner` is mandatory
and must exist; every run is safe to repeat (`legacy_imports` unique key per source+job id: repeats report "already imported"); only finished jobs are imported (`done` with a validated file →
`completed`, `failed`/`canceled` → history entries), pending/running/paused jobs are **not** resurrected; files must be regular, non-symlink, of an allowed media type and **inside `--files-root`**
(otherwise skipped with the reason) and are **copied**, never moved; URLs must pass the current source policy; quota is respected; the legacy `command`, `cookies_browser` and raw error text are never imported
(failed jobs get a generic message). Imported files follow the normal retention (they expire after `COMPLETED_RETENTION_HOURS`), so save them promptly.
In Docker, run it in the **worker** image (the API container's media mount is read-only) with your legacy directory bind-mounted read-write (the backup is written beside the source):
`docker compose run --rm -v ~/legacy:/legacy worker python -m app.cli import-legacy --sqlite /legacy/jobs.sqlite3 --owner me@example.com --files-root /legacy/downloads --dry-run`.
