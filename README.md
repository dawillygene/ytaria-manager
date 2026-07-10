# ytaria-manager

Local background download manager for YouTube-style URLs using `yt-dlp` and `aria2c`.

## What it does

- Queues jobs in SQLite
- Runs downloads in the background
- Uses `yt-dlp -f "bv*+ba" --downloader aria2c --merge-output-format mp4`
- Exposes a local web UI
- Exposes a curses TUI that reads the same queue

## Requirements

- `python3`
- `yt-dlp`
- `aria2c`
- `ffmpeg`

## Run the web app

```bash
cd /home/dawilly/Downloads
python3 ytaria-manager/ytaria.py serve --host 127.0.0.1 --port 8787
```

To detach into the background:

```bash
python3 ytaria-manager/ytaria.py serve --host 127.0.0.1 --port 8787 --background
```

Open:

- `http://127.0.0.1:8787/`

## Run the TUI

```bash
cd /home/dawilly/Downloads
python3 ytaria-manager/ytaria.py tui
```

Keys:

- `a` add a URL
- `r` refresh
- `q` quit

## CLI queueing

```bash
python3 ytaria-manager/ytaria.py add "https://youtu.be/tYp-UskX0cM"
python3 ytaria-manager/ytaria.py list
```

## Job controls

- **Pause / Continue** a running download (aria2c resumes from where it stopped)
- **Cancel** a queued or running job
- **Retry** a failed or canceled job
- Live progress, speed, and ETA per job

## Storage

- SQLite DB: `~/.local/share/ytaria-manager/jobs.sqlite3`
- Default output dir: `~/Downloads/ytaria-downloads`

---

Developed and maintained by **dawillygene**.

# ytaria-manager
