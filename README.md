# ytaria-manager

Local YouTube-style download manager built on `yt-dlp`, `aria2c`, and SQLite.

![ytaria-manager UI](assets/ytaria-manager-ui.png)

## Overview

`ytaria-manager` gives you a simple local interface for queueing video downloads, watching live progress, and controlling jobs without using the terminal for every action.

It includes:

- A local web UI for queueing and managing downloads
- A shared SQLite job queue
- A background worker powered by `yt-dlp` and `aria2c`
- A curses TUI that reads the same jobs as the web app
- Pause, resume, cancel, and retry controls

## Stack

- `python3`
- `yt-dlp`
- `aria2c`
- `ffmpeg`
- `sqlite3`

## Requirements

Make sure these are installed and available in your shell:

- `python3`
- `yt-dlp`
- `aria2c`
- `ffmpeg`

## Start The Web UI

Run in the foreground:

```bash
cd /home/dawilly/Downloads/ytaria-manager
python3 ytaria.py serve --host 127.0.0.1 --port 8787
```

Start it in the background:

```bash
cd /home/dawilly/Downloads/ytaria-manager
python3 ytaria.py start --host 127.0.0.1 --port 8787
```

Open:

- `http://127.0.0.1:8787/`

Check status, stop, or restart:

```bash
cd /home/dawilly/Downloads/ytaria-manager
python3 ytaria.py status --host 127.0.0.1 --port 8787
python3 ytaria.py stop --host 127.0.0.1 --port 8787
python3 ytaria.py restart --host 127.0.0.1 --port 8787
```

Shell helpers:

```bash
./start-web.sh
./status-web.sh
./stop-web.sh
./restart-web.sh
```

The background service keeps its PID in `~/.local/state/ytaria-manager/web.pid`.

## Start The TUI

```bash
cd /home/dawilly/Downloads/ytaria-manager
python3 ytaria.py tui
```

Keys:

- `a` add a URL
- `r` refresh
- `q` quit

## Queue Jobs From CLI

```bash
python3 ytaria.py add "https://youtu.be/tYp-UskX0cM"
python3 ytaria.py list
```

## Bypassing YouTube's Bot Check

YouTube increasingly blocks anonymous downloads with:

```
ERROR: [youtube] ...: Sign in to confirm you're not a bot.
```

To get past it, `yt-dlp` needs cookies from a logged-in browser session. You have two ways to supply them.

### Per-job, from the web UI

Next to the URL field there's a **Cookies from browser** menu. Pick your logged-in
browser (Firefox, Chrome, Chromium, Brave, Edge, Opera, Vivaldi, or Safari) before
adding the job. The choice is saved with the job.

Every failed or canceled job card also has its own browser menu next to **Retry**, so
you can add cookies to a job that was queued before you set a browser (or switch to a
different one) and retry it without re-pasting the URL.

From the CLI, use the matching flag:

```bash
python3 ytaria.py add "https://youtu.be/..." --cookies-from-browser firefox
```

### Globally, with environment variables

Set one of these before launching `serve`, `worker`, or `tui`. They act as the default
for any job that doesn't specify its own browser:

```bash
export YTARIA_COOKIES_FROM_BROWSER=firefox     # read cookies straight from a browser
export YTARIA_COOKIES_FILE=/path/to/cookies.txt # or an exported cookies.txt
```

**Notes**

- Fully close Chromium-based browsers (Chrome, Brave, Edge, …) while downloading, or
  `yt-dlp` will hit a locked cookie database. Firefox doesn't have this problem.
- `YTARIA_COOKIES_FROM_BROWSER` takes precedence over `YTARIA_COOKIES_FILE`; a per-job
  browser choice overrides both.
- Use an account you don't mind exercising through a downloader.

## Download Behavior

The worker runs downloads with:

```bash
yt-dlp -f "bv*+ba/b" \
  --downloader aria2c \
  --merge-output-format mp4
```

The `/b` fallback matters: it takes the best separate video+audio streams when they
exist, but falls back to the best single combined file otherwise. Without it, videos
that only offer progressive formats fail with `Requested format is not available`.

## Job Controls

- `Pause` stops the active download and keeps resume data
- `Continue` requeues a paused job and resumes it
- `Cancel` stops a queued or running job
- `Retry` restarts a failed or canceled job
- Live progress, speed, ETA, and output path are shown in the UI

## Storage

- SQLite database: `~/.local/share/ytaria-manager/jobs.sqlite3`
- Default output directory: `~/Downloads/ytaria-downloads`

## Project Files

- [ytaria.py](ytaria.py) main application with web UI, worker, API, and TUI
- [start-web.sh](start-web.sh) helper to start the web app in background mode
- [stop-web.sh](stop-web.sh) helper to stop the background web app
- [restart-web.sh](restart-web.sh) helper to restart the background web app
- [status-web.sh](status-web.sh) helper to inspect the background web app
- [start-tui.sh](start-tui.sh) helper to launch the terminal UI

## Credit

Developed and maintained by **Elia William Mariki (dawillygene)**, a systems software engineer based in Dodoma, Tanzania.

Website: [dawillygene.com](https://www.dawillygene.com/)
