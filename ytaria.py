#!/usr/bin/env python3
"""Local yt-dlp + aria2 download manager with web and TUI frontends."""

from __future__ import annotations

import argparse
import curses
import fcntl
import json
import os
import pty
import re
import shlex
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


APP_NAME = "ytaria-manager"
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "ytaria-downloads"
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / APP_NAME / "jobs.sqlite3"

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELED = "canceled"
JOB_PAUSED = "paused"

PROGRESS_RE = re.compile(r"(?P<pct>\d+(?:\.\d+)?)%")
DESTINATION_RE = re.compile(r"Destination:\s*(?P<name>.+)$")
MERGER_RE = re.compile(r'Merging formats into "(?P<name>.+)"')
# Strip terminal control codes that aria2c emits when it thinks it has a tty.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# aria2c console readout, e.g. "[#e771c3 174MiB/590MiB(29%) CN:16 DL:2.3MiB ETA:2m57s]"
ARIA_DL_RE = re.compile(r"DL:\s*([0-9.]+\s*[KMGT]?i?B)")
ARIA_ETA_RE = re.compile(r"ETA:\s*([0-9dhms]+)")
# yt-dlp's own progress line, e.g. "100% of 23.08MiB in 00:00:05 at 4.16MiB/s"
YTDLP_SPEED_RE = re.compile(r"at\s+([0-9.]+\s*[KMGT]?i?B/s)")
YTDLP_ETA_RE = re.compile(r"ETA\s+([0-9:]+)")
# Number of trailing output lines kept so a failed job explains itself.
ERROR_TAIL_LINES = 20


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def title_from_destination(path: str) -> str:
    """Derive a readable title from a yt-dlp destination filename.

    With --restrict-filenames the file is ``<sanitized title>-[<id>].<ext>``
    (video/audio streams also carry a ``.fNNN`` format suffix), so strip those
    and turn separators back into spaces for display.
    """
    name = Path(path).name
    name = re.sub(r"\.f\d+(?=\.[^.]+$)", "", name)  # drop .f399 format tag
    name = re.sub(r"\.[^.]+$", "", name)  # drop extension
    name = re.sub(r"-\[[^\]]+\]$", "", name)  # drop -[videoid]
    name = name.replace("_", " ").strip()
    return name


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def open_db(db_path: Path) -> sqlite3.Connection:
    ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            output_dir TEXT NOT NULL,
            command TEXT NOT NULL,
            status TEXT NOT NULL,
            progress REAL NOT NULL DEFAULT 0,
            speed TEXT NOT NULL DEFAULT '',
            eta TEXT NOT NULL DEFAULT '',
            destination TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.commit()


def dict_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


class JobStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = open_db(db_path)
        init_db(self.conn)
        self.lock = threading.Lock()

    def add_job(self, url: str, output_dir: Path, command: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                """
                INSERT INTO jobs (url, output_dir, command, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (url, str(output_dir), command, JOB_PENDING, now_iso()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict_from_row(row) for row in rows]

    def get_job(self, job_id: int) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict_from_row(row) if row else None

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        keys = list(fields.keys())
        values = [fields[key] for key in keys]
        assignments = ", ".join(f"{key} = ?" for key in keys)
        with self.lock:
            self.conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*values, job_id),
            )
            self.conn.commit()

    def requeue_orphans(self) -> int:
        """Reset jobs left 'running' by a crashed/killed worker back to pending."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status = ?, progress = 0, speed = '', eta = '', "
                "started_at = NULL WHERE status = ?",
                (JOB_PENDING, JOB_RUNNING),
            )
            self.conn.commit()
            return cur.rowcount

    def retry_job(self, job_id: int) -> bool:
        """Requeue a failed or canceled job from scratch."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status = ?, progress = 0, speed = '', eta = '', "
                "error = '', destination = '', started_at = NULL, finished_at = NULL "
                "WHERE id = ? AND status IN (?, ?)",
                (JOB_PENDING, job_id, JOB_FAILED, JOB_CANCELED),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def cancel_pending(self, job_id: int) -> bool:
        """Cancel a job that isn't actively running (no live process to stop)."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status = ?, speed = '', eta = '', "
                "finished_at = ?, error = 'Canceled.' WHERE id = ? AND status IN (?, ?)",
                (JOB_CANCELED, now_iso(), job_id, JOB_PENDING, JOB_PAUSED),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def resume_job(self, job_id: int) -> bool:
        """Requeue a paused job, keeping its progress so aria2c resumes."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE jobs SET status = ?, speed = '', eta = '', error = '', "
                "finished_at = NULL WHERE id = ? AND status = ?",
                (JOB_PENDING, job_id, JOB_PAUSED),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def claim_next_pending(self) -> Optional[dict[str, Any]]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id ASC LIMIT 1",
                (JOB_PENDING,),
            ).fetchone()
            if not row:
                self.conn.commit()
                return None
            self.conn.execute(
                "UPDATE jobs SET status = ?, started_at = ?, progress = 0, speed = '', eta = '', error = '' WHERE id = ?",
                (JOB_RUNNING, now_iso(), row["id"]),
            )
            self.conn.commit()
        return self.get_job(int(row["id"]))


def build_yt_dlp_command(url: str, output_dir: Path) -> list[str]:
    return [
        "yt-dlp",
        "-f",
        "bv*+ba",
        "--downloader",
        "aria2c",
        # Keep transient network hiccups from killing a job outright.
        "--downloader-args",
        "aria2c:--max-tries=10 --retry-wait=3 --timeout=60 --connect-timeout=30",
        "--retries",
        "infinite",
        "--fragment-retries",
        "infinite",
        "--merge-output-format",
        "mp4",
        "--newline",
        "--no-playlist",
        "--restrict-filenames",
        "-P",
        str(output_dir),
        url,
    ]


@dataclass
class CurrentJob:
    job_id: int
    process: "subprocess.Popen[bytes]"
    cancel_requested: bool = False
    pause_requested: bool = False


class Worker:
    def __init__(self, store: JobStore):
        self.store = store
        self.current: Optional[CurrentJob] = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="ytaria-worker", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.current:
            self._kill_group(self.current.process)

    @staticmethod
    def _kill_group(process: "subprocess.Popen[bytes]", sig: int = signal.SIGTERM) -> None:
        # yt-dlp spawns aria2c as a child; signalling the whole process group
        # is the only way to stop the download instead of orphaning aria2c.
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, OSError):
            try:
                process.send_signal(sig)
            except OSError:
                pass

    def request_cancel(self, job_id: int) -> bool:
        if self.current and self.current.job_id == job_id:
            self.current.cancel_requested = True
            self._kill_group(self.current.process)
            return True

        job = self.store.get_job(job_id)
        if not job or job["status"] not in {JOB_PENDING, JOB_RUNNING, JOB_PAUSED}:
            return False

        self.store.update_job(
            job_id,
            status=JOB_CANCELED,
            speed="",
            eta="",
            finished_at=now_iso(),
            error="Canceled before start.",
        )
        return True

    def request_pause(self, job_id: int) -> bool:
        # Only the actively running job can be paused; terminating aria2c
        # leaves its .aria2 control file so the download resumes later.
        if self.current and self.current.job_id == job_id:
            self.current.pause_requested = True
            self._kill_group(self.current.process)
            return True
        return False

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.store.claim_next_pending()
            except sqlite3.OperationalError:
                time.sleep(1)
                continue
            if not job:
                time.sleep(1)
                continue

            self._run_job(job)

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        output_dir = Path(job["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = build_yt_dlp_command(job["url"], output_dir)
        command_text = shlex.join(cmd)
        self.store.update_job(job_id, command=command_text)

        # Run under a pseudo-terminal so aria2c streams its progress readout
        # in real time (it stays silent when it detects a plain pipe). The
        # window size must be non-zero or aria2c truncates the readout away.
        master_fd, slave_fd = pty.openpty()
        try:
            termios_winsize = struct.pack("HHHH", 50, 220, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, termios_winsize)
        except OSError:
            pass
        process = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=slave_fd,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,  # own process group so we can kill aria2c too
        )
        os.close(slave_fd)
        self.current = CurrentJob(job_id=job_id, process=process)
        tail: deque[str] = deque(maxlen=ERROR_TAIL_LINES)

        try:
            buf = b""
            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break  # pty closed when the child exits
                if not chunk:
                    break
                # aria2c overwrites its readout with \r; treat CR as a line break
                # so each progress update is processed the moment it arrives.
                buf = (buf + chunk).replace(b"\r", b"\n")
                *complete, buf = buf.split(b"\n")
                for raw in complete:
                    line = ANSI_RE.sub("", raw.decode("utf-8", "replace")).rstrip()
                    if not line:
                        continue
                    if not line.startswith("[#"):  # skip progress readouts in log tail
                        tail.append(line)
                    self._consume_output(job_id, line)
                if self.current.cancel_requested or self.current.pause_requested:
                    break

            os.close(master_fd)
            return_code = process.wait()
            if self.current.cancel_requested:
                self.store.update_job(
                    job_id, status=JOB_CANCELED, speed="", eta="",
                    finished_at=now_iso(), error="Canceled by user.",
                )
            elif self.current.pause_requested:
                # Keep progress/destination; the .aria2 file lets it resume.
                self.store.update_job(
                    job_id, status=JOB_PAUSED, speed="", eta="", error="",
                )
            elif return_code == 0:
                self.store.update_job(
                    job_id, status=JOB_DONE, progress=100, speed="", eta="",
                    finished_at=now_iso(), error="",
                )
            elif return_code == -signal.SIGTERM:
                self.store.update_job(
                    job_id, status=JOB_CANCELED, speed="", eta="",
                    finished_at=now_iso(), error="Stopped.",
                )
            else:
                self.store.update_job(
                    job_id, status=JOB_FAILED, speed="", eta="",
                    finished_at=now_iso(), error=self._format_failure(return_code, tail),
                )
        except Exception as exc:  # pragma: no cover - defensive guard
            try:
                os.close(master_fd)
            except OSError:
                pass
            self.store.update_job(
                job_id, status=JOB_FAILED, speed="", eta="",
                finished_at=now_iso(), error=str(exc),
            )
        finally:
            self.current = None

    @staticmethod
    def _format_failure(return_code: int, tail: "deque[str]") -> str:
        # Prefer the actual yt-dlp/aria2c error lines so the UI is diagnosable.
        errors = [ln for ln in tail if ln.lstrip().startswith("ERROR")]
        detail = "\n".join(errors or list(tail)[-8:])
        header = f"yt-dlp exited with code {return_code}."
        return f"{header}\n{detail}".strip() if detail else header

    def _consume_output(self, job_id: int, line: str) -> None:
        updates: dict[str, Any] = {}
        pct = PROGRESS_RE.search(line)
        if pct:
            try:
                updates["progress"] = float(pct.group("pct"))
            except ValueError:
                pass

        destination = DESTINATION_RE.search(line)
        merger = MERGER_RE.search(line)
        dest_path = None
        if merger:
            dest_path = merger.group("name").strip()
        elif destination:
            dest_path = destination.group("name").strip()
        if dest_path:
            updates["destination"] = dest_path
            title = title_from_destination(dest_path)
            if title:
                updates["title"] = title

        # Speed/ETA: aria2c readout (DL:/ETA:) first, then yt-dlp's own line.
        dl = ARIA_DL_RE.search(line)
        if dl:
            updates["speed"] = dl.group(1).replace(" ", "") + "/s"
        else:
            yt_speed = YTDLP_SPEED_RE.search(line)
            if yt_speed:
                updates["speed"] = yt_speed.group(1).replace(" ", "")

        aria_eta = ARIA_ETA_RE.search(line)
        if aria_eta:
            updates["eta"] = aria_eta.group(1)
        else:
            yt_eta = YTDLP_ETA_RE.search(line)
            if yt_eta:
                updates["eta"] = yt_eta.group(1)

        if updates:
            self.store.update_job(job_id, **updates)


class APIHandler(BaseHTTPRequestHandler):
    server_version = "ytaria/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_index()
            return
        if parsed.path == "/api/jobs":
            self._send_json({"jobs": self.server.app.store.list_jobs()})  # type: ignore[attr-defined]
            return
        if parsed.path.startswith("/api/jobs/"):
            self._serve_job_detail(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            self._create_job()
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
            self._cancel_job(parsed.path)
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/retry"):
            self._retry_job(parsed.path)
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/pause"):
            self._pause_job(parsed.path)
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/resume"):
            self._resume_job(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve_index(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def _serve_job_detail(self, path: str) -> None:
        match = re.fullmatch(r"/api/jobs/(\d+)", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job_id = int(match.group(1))
        job = self.server.app.store.get_job(job_id)  # type: ignore[attr-defined]
        if not job:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_json(job)

    def _cancel_job(self, path: str) -> None:
        match = re.fullmatch(r"/api/jobs/(\d+)/cancel", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job_id = int(match.group(1))
        worker = self.server.app.worker  # type: ignore[attr-defined]
        if worker is not None:
            ok = worker.request_cancel(job_id)
        else:
            ok = self.server.app.store.cancel_pending(job_id)  # type: ignore[attr-defined]
        if not ok:
            self.send_error(HTTPStatus.CONFLICT, "job is not cancelable")
            return
        self._send_json({"ok": True})

    def _retry_job(self, path: str) -> None:
        match = re.fullmatch(r"/api/jobs/(\d+)/retry", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job_id = int(match.group(1))
        ok = self.server.app.store.retry_job(job_id)  # type: ignore[attr-defined]
        if not ok:
            self.send_error(HTTPStatus.CONFLICT, "job is not retryable")
            return
        self._send_json({"ok": True})

    def _pause_job(self, path: str) -> None:
        match = re.fullmatch(r"/api/jobs/(\d+)/pause", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job_id = int(match.group(1))
        worker = self.server.app.worker  # type: ignore[attr-defined]
        ok = worker.request_pause(job_id) if worker is not None else False
        if not ok:
            self.send_error(HTTPStatus.CONFLICT, "job is not running")
            return
        self._send_json({"ok": True})

    def _resume_job(self, path: str) -> None:
        match = re.fullmatch(r"/api/jobs/(\d+)/resume", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        job_id = int(match.group(1))
        ok = self.server.app.store.resume_job(job_id)  # type: ignore[attr-defined]
        if not ok:
            self.send_error(HTTPStatus.CONFLICT, "job is not paused")
            return
        self._send_json({"ok": True})

    def _create_job(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        payload = json.loads(raw or "{}")
        url = str(payload.get("url", "")).strip()
        output_dir = Path(payload.get("output_dir") or str(DEFAULT_OUTPUT_DIR))
        if not url:
            self.send_error(HTTPStatus.BAD_REQUEST, "url is required")
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        command = shlex.join(build_yt_dlp_command(url, output_dir))
        job_id = self.server.app.store.add_job(url=url, output_dir=output_dir, command=command)  # type: ignore[attr-defined]
        self._send_json({"ok": True, "id": job_id})

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AppServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], app: "App"):
        super().__init__(address, handler)
        self.app = app


class App:
    def __init__(self, db_path: Path):
        self.store = JobStore(db_path)
        self.worker: Optional[Worker] = None
        self._worker_lock: Optional[Any] = None

    def start_worker(self, db_path: Path) -> bool:
        """Start the in-process download worker if no other worker holds the lock."""
        self._worker_lock = acquire_worker_lock(db_path)
        if self._worker_lock is None:
            return False
        requeued = self.store.requeue_orphans()
        if requeued:
            print(f"Requeued {requeued} orphaned job(s).", flush=True)
        self.worker = Worker(self.store)
        self.worker.start()
        return True

    def stop_worker(self) -> None:
        if self.worker is not None:
            self.worker.stop()


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ytaria-manager</title>
  <style>
    :root {
      --bg: #0d1117;
      --panel: #161b22;
      --panel-2: #0f1720;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #2f81f7;
      --good: #2ea043;
      --warn: #d29922;
      --bad: #f85149;
      --line: #30363d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(47,129,247,0.18), transparent 30%),
        radial-gradient(circle at bottom right, rgba(46,160,67,0.15), transparent 25%),
        var(--bg);
      color: var(--text);
    }
    .wrap { max-width: 1220px; margin: 0 auto; padding: 32px 20px 48px; }
    .hero {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
      margin-bottom: 20px;
    }
    h1 { margin: 0; font-size: 32px; letter-spacing: -0.03em; }
    .sub { color: var(--muted); margin-top: 8px; max-width: 62ch; line-height: 1.5; }
    .card {
      background: rgba(22,27,34,0.86);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 18px 45px rgba(0,0,0,0.28);
      backdrop-filter: blur(10px);
    }
    .grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(320px, 0.85fr); gap: 16px; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
    label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 8px; }
    input {
      width: 100%;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      outline: none;
    }
    input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(47,129,247,0.18); }
    .row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; }
    @media (max-width: 640px) { .row { grid-template-columns: 1fr; } }
    button {
      padding: 12px 16px;
      border: 0;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--accent), #6ea8fe);
      color: white;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary { background: var(--panel-2); border: 1px solid var(--line); color: var(--text); }
    .jobs-shell { padding: 0; overflow: hidden; }
    .jobs-head, .job-row {
      display: grid;
      grid-template-columns: minmax(0, 2.3fr) minmax(180px, 1fr) minmax(260px, 1.5fr) 118px;
      gap: 18px;
      align-items: start;
    }
    .jobs-head {
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .job-row {
      padding: 18px;
      border-bottom: 1px solid var(--line);
    }
    .job-row:last-child { border-bottom: 0; }
    .job-cell { min-width: 0; }
    .job-main {
      display: grid;
      grid-template-columns: 56px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }
    .job-id {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 34px;
      height: 34px;
      border-radius: 10px;
      background: rgba(255,255,255,0.04);
      font-weight: 700;
    }
    .job-title {
      font-size: 15px;
      line-height: 1.4;
      word-break: break-word;
    }
    .job-url,
    .job-output,
    .job-destination {
      margin-top: 6px;
      font-size: 12px;
      line-height: 1.45;
      word-break: break-word;
      overflow-wrap: anywhere;
    }
    .job-output { font-size: 13px; margin-top: 0; }
    .job-status-block {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .job-actions {
      display: flex;
      flex-direction: column;
      gap: 10px;
      align-items: stretch;
    }
    .job-actions button { width: 100%; }
    .progress-meta {
      margin-top: 8px;
      font-size: 12px;
      line-height: 1.4;
    }
    .empty-state {
      padding: 28px 18px;
      color: var(--muted);
      text-align: center;
    }
    @media (max-width: 1240px) {
      .jobs-head { display: none; }
      .job-row {
        grid-template-columns: 1fr;
        gap: 12px;
      }
      .job-main { grid-template-columns: 44px minmax(0, 1fr); }
      .job-actions {
        flex-direction: row;
        flex-wrap: wrap;
      }
      .job-actions button {
        width: auto;
        min-width: 110px;
      }
    }
    .pill {
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      background: rgba(255,255,255,0.06);
    }
    .pill.good { color: #9be9a8; }
    .pill.warn { color: #f2cc60; }
    .pill.bad { color: #ff7b72; }
    .muted { color: var(--muted); }
    .progress { width: 100%; height: 8px; border-radius: 999px; background: rgba(255,255,255,0.07); overflow: hidden; }
    .progress > span { display: block; height: 100%; }
    .bar-live { background: linear-gradient(90deg, #2f81f7, #2ea043); }
    .bar-done { background: var(--good); }
    .bar-failed { background: var(--bad); }
    .bar-canceled { background: var(--muted); }
    .bar-paused { background: var(--warn); opacity: 0.7; }
    .errbox {
      margin-top: 8px;
      padding: 8px 10px;
      border-radius: 8px;
      border: 1px solid rgba(248,81,73,0.4);
      background: rgba(248,81,73,0.08);
      color: #ff9a92;
      font-size: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 140px;
      overflow: auto;
    }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .statusline { margin-top: 8px; color: var(--muted); font-size: 13px; }
    .footer { margin-top: 18px; color: var(--muted); font-size: 13px; }
    .credit {
      margin-top: 10px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
      text-align: center;
      color: var(--muted);
      font-size: 13px;
      letter-spacing: 0.02em;
    }
    .credit strong { color: var(--accent); }
    code { color: #8fd3ff; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1>ytaria-manager</h1>
        <div class="sub">Queue YouTube jobs in the background. The worker runs <code>yt-dlp</code> with <code>aria2c</code>, merges audio and video, and keeps shared state in SQLite so the web UI and TUI see the same jobs.</div>
      </div>
      <div class="actions">
        <button class="secondary" onclick="refreshJobs()">Refresh</button>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <label for="url">Video URL</label>
        <div class="row">
          <input id="url" placeholder="https://youtu.be/..." autocomplete="off">
          <button onclick="submitJob()">Add job</button>
        </div>
        <div class="statusline" id="submitStatus">Ready.</div>
      </div>
      <div class="card">
        <label for="outputDir">Output directory</label>
        <div class="row">
          <input id="outputDir" value="__DEFAULT_OUTPUT_DIR__">
          <button class="secondary" onclick="setDefaultOutput()">Use default</button>
        </div>
        <div class="statusline">Default: <code>__DEFAULT_OUTPUT_DIR__</code></div>
      </div>
    </div>

    <div class="card jobs-shell" style="margin-top:16px;">
      <div class="jobs-head">
        <div>Job</div>
        <div>Status</div>
        <div>Output</div>
        <div>Actions</div>
      </div>
      <div id="jobs"></div>
    </div>

    <div class="footer">API: <code>/api/jobs</code>. TUI: run <code>python3 ytaria.py tui</code>.</div>
    <div class="credit">Developed and maintained by <strong>dawillygene</strong></div>
  </div>
  <script>
    const defaultOutput = __DEFAULT_OUTPUT_JSON__;
    function setDefaultOutput() {{
      document.getElementById('outputDir').value = defaultOutput;
    }}
    function pillClass(status) {{
      if (status === 'done') return 'pill good';
      if (status === 'running' || status === 'pending' || status === 'paused') return 'pill warn';
      if (status === 'failed' || status === 'canceled') return 'pill bad';
      return 'pill';
    }}
    function esc(s) {{
      return String(s || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}
    function fmtProgress(job) {{
      const running = job.status === 'running';
      const pct = job.status === 'done' ? 100 : Number(job.progress || 0);
      const clamped = Math.max(0, Math.min(100, pct));
      // Bar color tracks status so a dead job never looks live.
      let barClass = 'bar-live';
      if (job.status === 'done') barClass = 'bar-done';
      else if (job.status === 'failed') barClass = 'bar-failed';
      else if (job.status === 'canceled') barClass = 'bar-canceled';
      else if (job.status === 'paused') barClass = 'bar-paused';
      // Only a running job shows a live speed/ETA line.
      let detail = '';
      if (running) {{
        const parts = [];
        if (job.speed) parts.push(esc(job.speed));
        if (job.eta) parts.push('ETA ' + esc(job.eta));
        detail = parts.join(' • ');
      }} else if (job.status === 'paused') {{
        detail = 'paused';
      }}
      return `
        <div class="progress"><span class="${{barClass}}" style="width:${{clamped}}%"></span></div>
        <div class="muted progress-meta">${{pct.toFixed(1)}}%${{detail ? ' • ' + detail : ''}}</div>
      `;
    }}
    function fmtActions(job) {{
      const cancel = `<button class="secondary" onclick="cancelJob(${{job.id}})">Cancel</button>`;
      if (job.status === 'running') {{
        return `<button class="secondary" onclick="pauseJob(${{job.id}})">Pause</button> ` + cancel;
      }}
      if (job.status === 'paused') {{
        return `<button onclick="resumeJob(${{job.id}})">Continue</button> ` + cancel;
      }}
      if (job.status === 'pending') {{
        return cancel;
      }}
      if (job.status === 'failed' || job.status === 'canceled') {{
        return `<button class="secondary" onclick="retryJob(${{job.id}})">Retry</button>`;
      }}
      return '<span class="muted">—</span>';
    }}
    async function refreshJobs() {{
      const res = await fetch('/api/jobs');
      const data = await res.json();
      const jobsEl = document.getElementById('jobs');
      jobsEl.innerHTML = data.jobs.map(job => {{
        const hasTitle = job.title && job.title !== job.url;
        const err = job.status === 'failed' && job.error
          ? `<div class="errbox">${{esc(job.error)}}</div>` : '';
        return `
        <div class="job-row">
          <div class="job-cell">
            <div class="job-main">
              <span class="job-id">${{job.id}}</span>
              <div>
                <div class="job-title">${{hasTitle ? esc(job.title) : esc(job.url)}}</div>
                ${{hasTitle ? `<div class="muted job-url">${{esc(job.url)}}</div>` : ''}}
                ${{err}}
              </div>
            </div>
          </div>
          <div class="job-cell">
            <div class="job-status-block">
              <span class="${{pillClass(job.status)}}">${{esc(job.status)}}</span>
              <div>${{fmtProgress(job)}}</div>
            </div>
          </div>
          <div class="job-cell">
            <div class="job-output">${{esc(job.output_dir)}}</div>
            <div class="muted job-destination">${{esc(job.destination || '')}}</div>
          </div>
          <div class="job-cell job-actions">${{fmtActions(job)}}</div>
        </div>
      `;
      }}).join('') || '<div class="empty-state">No jobs yet.</div>';
    }}
    async function submitJob() {{
      const url = document.getElementById('url').value.trim();
      const output_dir = document.getElementById('outputDir').value.trim();
      const status = document.getElementById('submitStatus');
      if (!url) {{
        status.textContent = 'URL is required.';
        return;
      }}
      status.textContent = 'Submitting job...';
      const res = await fetch('/api/jobs', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{url, output_dir}})
      }});
      if (!res.ok) {{
        status.textContent = 'Failed to submit job.';
        return;
      }}
      const data = await res.json();
      status.textContent = 'Queued job #' + data.id;
      document.getElementById('url').value = '';
      await refreshJobs();
    }}
    async function cancelJob(id) {{
      await fetch(`/api/jobs/${{id}}/cancel`, {{method: 'POST'}});
      await refreshJobs();
    }}
    async function retryJob(id) {{
      await fetch(`/api/jobs/${{id}}/retry`, {{method: 'POST'}});
      await refreshJobs();
    }}
    async function pauseJob(id) {{
      await fetch(`/api/jobs/${{id}}/pause`, {{method: 'POST'}});
      await refreshJobs();
    }}
    async function resumeJob(id) {{
      await fetch(`/api/jobs/${{id}}/resume`, {{method: 'POST'}});
      await refreshJobs();
    }}
    refreshJobs();
    setInterval(refreshJobs, 1500);
  </script>
</body>
</html>
"""

HTML_PAGE = HTML_PAGE.replace("__DEFAULT_OUTPUT_DIR__", str(DEFAULT_OUTPUT_DIR)).replace(
    "__DEFAULT_OUTPUT_JSON__", json.dumps(str(DEFAULT_OUTPUT_DIR))
).replace("{{", "{").replace("}}", "}")


def daemonize() -> None:
    if os.name != "posix":
        raise RuntimeError("daemonize is only supported on POSIX systems")
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)
    sys.stdin.flush()
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r") as devnull_r, open(os.devnull, "a+") as devnull_w:
        os.dup2(devnull_r.fileno(), 0)
        os.dup2(devnull_w.fileno(), 1)
        os.dup2(devnull_w.fileno(), 2)


def serve(args: argparse.Namespace) -> None:
    if args.background:
        daemonize()

    app = App(Path(args.db))
    # Run the worker in-process (as a thread) so the web UI can pause/cancel
    # the live download directly; the lock keeps a single downloader.
    if app.start_worker(Path(args.db)):
        print("Download worker started.", flush=True)
    else:
        print("Another worker already owns the queue; serving UI only.", flush=True)

    server = AppServer((args.host, args.port), APIHandler, app)

    def shutdown(*_: Any) -> None:
        server.shutdown()
        app.stop_worker()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"ytaria-manager listening on http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app.stop_worker()


def acquire_worker_lock(db_path: Path) -> Optional[Any]:
    """Hold an exclusive lock so only one worker ever downloads at a time.

    Multiple concurrent workers share the DB and output dir, and their
    aria2c processes collide on partial/control files, which surfaces as
    spurious 'exited with code 1' failures. Returns the held file handle,
    or None if another worker already owns the lock.
    """
    lock_path = db_path.with_suffix(db_path.suffix + ".worker.lock")
    ensure_parent_dir(lock_path)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def worker_main(args: argparse.Namespace) -> None:
    lock = acquire_worker_lock(Path(args.db))
    if lock is None:
        print("Another worker is already running; exiting.", flush=True)
        return
    store = JobStore(Path(args.db))
    requeued = store.requeue_orphans()
    if requeued:
        print(f"Requeued {requeued} orphaned job(s).", flush=True)
    worker = Worker(store)
    worker.run()


def tui(args: argparse.Namespace) -> None:
    store = JobStore(Path(args.db))

    def draw(stdscr: Any) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(1000)
        input_mode = False
        buffer = ""
        message = "Press a to add, r to refresh, q to quit."
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            jobs = store.list_jobs(limit=max(5, height - 8))

            stdscr.addstr(0, 0, "ytaria-manager TUI"[: max(0, width - 1)], curses.A_BOLD)
            stdscr.addstr(1, 0, message[: max(0, width - 1)])
            stdscr.addstr(2, 0, ("URL: " + buffer)[: max(0, width - 1)])
            stdscr.hline(3, 0, ord("-"), max(0, width - 1))

            row = 4
            header = f"{'ID':<5} {'STATUS':<10} {'PROGRESS':<9} {'URL':<48}"
            stdscr.addstr(row, 0, header[: max(0, width - 1)], curses.A_UNDERLINE)
            row += 1
            for job in jobs:
                if row >= height - 2:
                    break
                pct = f"{float(job['progress'] or 0):5.1f}%"
                status = str(job["status"])[:10]
                url = str(job["url"])[: max(0, width - 30)]
                line = f"{job['id']:<5} {status:<10} {pct:<9} {url}"
                attr = curses.A_NORMAL
                if job["status"] == JOB_DONE:
                    attr = curses.color_pair(2)
                elif job["status"] in {JOB_FAILED, JOB_CANCELED}:
                    attr = curses.color_pair(1)
                elif job["status"] == JOB_RUNNING:
                    attr = curses.color_pair(3)
                stdscr.addstr(row, 0, line[: max(0, width - 1)], attr)
                row += 1

            stdscr.refresh()
            ch = stdscr.getch()
            if ch == -1:
                continue
            if input_mode:
                if ch in (10, 13):
                    if buffer.strip():
                        command = shlex.join(build_yt_dlp_command(buffer.strip(), DEFAULT_OUTPUT_DIR))
                        job_id = store.add_job(buffer.strip(), DEFAULT_OUTPUT_DIR, command)
                        message = f"Queued job #{job_id}."
                    buffer = ""
                    input_mode = False
                elif ch in (27,):
                    buffer = ""
                    input_mode = False
                    message = "Add canceled."
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    buffer = buffer[:-1]
                elif 32 <= ch <= 126:
                    buffer += chr(ch)
                continue

            if ch in (ord("q"), ord("Q")):
                break
            if ch in (ord("r"), ord("R")):
                message = "Refreshed."
            if ch in (ord("a"), ord("A")):
                input_mode = True
                buffer = ""
                message = "Enter a URL and press Enter."

    curses.wrapper(_init_curses_and_draw, draw)


def _init_curses_and_draw(stdscr: Any, draw_fn: Any) -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    draw_fn(stdscr)


def add_job(args: argparse.Namespace) -> None:
    store = JobStore(Path(args.db))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    command = shlex.join(build_yt_dlp_command(args.url, output_dir))
    job_id = store.add_job(args.url, output_dir, command)
    print(job_id)


def list_jobs(args: argparse.Namespace) -> None:
    store = JobStore(Path(args.db))
    jobs = store.list_jobs(limit=args.limit)
    for job in jobs:
        print(
            f"{job['id']:>4} {job['status']:<10} {float(job['progress'] or 0):>6.1f}% "
            f"{job['url']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")

    sub = parser.add_subparsers(dest="cmd", required=True)

    serve_p = sub.add_parser("serve", help="Run the web UI and background worker")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8787)
    serve_p.add_argument("--background", action="store_true", help="Detach into the background")
    serve_p.set_defaults(func=serve)

    worker_p = sub.add_parser("worker", help="Run only the background download worker")
    worker_p.set_defaults(func=worker_main)

    tui_p = sub.add_parser("tui", help="Run the terminal UI")
    tui_p.set_defaults(func=tui)

    add_p = sub.add_parser("add", help="Queue a job from the CLI")
    add_p.add_argument("url")
    add_p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    add_p.set_defaults(func=add_job)

    list_p = sub.add_parser("list", help="List jobs")
    list_p.add_argument("--limit", type=int, default=20)
    list_p.set_defaults(func=list_jobs)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
