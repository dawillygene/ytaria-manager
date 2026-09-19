#!/usr/bin/env python3
"""Legacy local (single-user) yt-dlp + aria2 download manager with web and TUI frontends.

The multi-user server and Android app live in backend/ and frontend/; see docs/LEGACY.md."""

from __future__ import annotations

import argparse
import atexit
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
DEFAULT_PID_PATH = Path.home() / ".local" / "state" / APP_NAME / "web.pid"

# LEGACY LOCAL MODE. The multi-user server lives in backend/ (see docs/LEGACY.md). This single-user
# script intentionally has NO browser-cookie / cookies.txt support: a tool must not read another
# browser profile or reuse one personal account for every download.
MAX_URL_LENGTH = 2048
MAX_BODY_BYTES = 64 * 1024

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


def validate_url(raw: str) -> str:
    """Accept only plain http(s) URLs. Blocks option injection (leading "-") and other schemes."""
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL_LENGTH or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValueError("invalid URL")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http:// and https:// URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not accepted")
    return url


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


def read_pid(pid_path: Path) -> Optional[int]:
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_looks_like_ytaria(pid: int) -> bool:
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    if not proc_cmdline.exists():
        return True
    try:
        cmdline = proc_cmdline.read_text(encoding="utf-8").replace("\x00", " ")
    except OSError:
        return False
    script_name = Path(__file__).name
    return script_name in cmdline and "serve" in cmdline


def find_service_pid_by_cmdline(host: str, port: int) -> Optional[int]:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return None
    script_name = Path(__file__).name
    host_flag = f"--host {host}"
    port_flag = f"--port {port}"
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_text(encoding="utf-8").replace("\x00", " ")
        except OSError:
            continue
        if script_name not in cmdline or " serve " not in f" {cmdline} ":
            continue
        if host_flag in cmdline and port_flag in cmdline:
            return int(entry.name)
    return None


def get_running_pid(pid_path: Path) -> Optional[int]:
    pid = read_pid(pid_path)
    if pid is None:
        return None
    if not is_pid_alive(pid):
        remove_pidfile(pid_path)
        return None
    if not pid_looks_like_ytaria(pid):
        return None
    return pid


def get_service_pid(pid_path: Path, host: str, port: int) -> tuple[Optional[int], bool]:
    managed_pid = get_running_pid(pid_path)
    if managed_pid is not None:
        return managed_pid, True
    discovered_pid = find_service_pid_by_cmdline(host, port)
    if discovered_pid is not None and is_pid_alive(discovered_pid):
        return discovered_pid, False
    return None, False


def write_pidfile(pid_path: Path) -> None:
    ensure_parent_dir(pid_path)
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def remove_pidfile(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def stop_pid(pid: int, timeout: float = 10.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return not is_pid_alive(pid)


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
            cookies_browser TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    # Column kept only so databases created by older versions keep working; it is never read or written.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "cookies_browser" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN cookies_browser TEXT NOT NULL DEFAULT ''")
    conn.commit()


def dict_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data.pop("command", None)  # never expose the command line
    data.pop("cookies_browser", None)
    return data


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
        "--ignore-config",  # do not inherit ~/.config/yt-dlp or system configuration
        "-f",
        # Best video+audio when separate streams exist, else the best single
        # combined file. Without the "/b" fallback, videos that only offer
        # progressive formats fail with "Requested format is not available".
        "bv*+ba/b",
        "--downloader",
        "aria2c",
        "--downloader-args",
        "aria2c:--max-tries=5 --retry-wait=3 --timeout=60 --connect-timeout=30",
        # Bounded retries (the original used "infinite", so a dead source never failed).
        "--retries",
        "5",
        "--fragment-retries",
        "5",
        "--merge-output-format",
        "mp4",
        "--newline",
        "--no-playlist",
        "--restrict-filenames",
        "-P",
        str(output_dir),
        "--",  # everything after this is the URL, never an option
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
        try:
            cmd = build_yt_dlp_command(validate_url(job["url"]), output_dir)
        except ValueError as exc:  # e.g. a row queued by an older, unvalidated version
            self.store.update_job(job_id, status=JOB_FAILED, finished_at=now_iso(), error=f"Rejected URL: {exc}")
            return
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
        store = self.server.app.store  # type: ignore[attr-defined]
        ok = store.retry_job(job_id)
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

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return json.loads(raw or "{}")

    def _create_job(self) -> None:
        try:
            payload = self._read_json()
            url = validate_url(str(payload.get("url", "")))
        except (ValueError, TypeError, AttributeError) as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        # The output directory is never taken from the client (it used to be, which allowed writes anywhere).
        output_dir = DEFAULT_OUTPUT_DIR
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
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {{
      theme: {{
        extend: {{
          boxShadow: {{
            glow: '0 24px 80px rgba(15, 23, 42, 0.45)',
          }},
        }},
      }},
    }};
  </script>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(14, 165, 233, 0.18), transparent 30%),
        radial-gradient(circle at bottom right, rgba(34, 197, 94, 0.14), transparent 28%),
        #020617;
    }}
  </style>
</head>
<body class="text-slate-100">
  <div class="min-h-screen">
    <div class="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
      <div class="mb-8 flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
        <div class="max-w-3xl">
          <div class="mb-3 inline-flex items-center gap-2 rounded-full border border-sky-400/20 bg-sky-400/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.22em] text-sky-200">
            Local download manager
          </div>
          <h1 class="text-4xl font-black tracking-tight text-white sm:text-5xl">ytaria-manager</h1>
          <p class="mt-4 text-base leading-8 text-slate-300 sm:text-lg">
            Queue YouTube jobs in the background. The worker runs <code class="rounded bg-slate-900/80 px-1.5 py-0.5 text-sky-300">yt-dlp</code>
            with <code class="rounded bg-slate-900/80 px-1.5 py-0.5 text-sky-300">aria2c</code>, merges audio and video,
            and keeps shared state in SQLite so the web UI and TUI stay in sync.
          </p>
        </div>
        <div class="flex shrink-0 items-center gap-3">
          <button
            class="inline-flex items-center justify-center rounded-2xl border border-white/10 bg-white/5 px-5 py-3 text-sm font-semibold text-slate-100 transition hover:border-sky-300/40 hover:bg-sky-400/10"
            onclick="refreshJobs()"
          >
            Refresh
          </button>
        </div>
      </div>

      <div class="grid gap-5 lg:grid-cols-[minmax(0,1.2fr)_minmax(340px,0.8fr)]">
        <section class="rounded-3xl border border-white/10 bg-slate-900/75 p-5 shadow-glow backdrop-blur">
          <label for="url" class="mb-3 block text-sm font-medium text-slate-300">Video URL</label>
          <div class="flex flex-col gap-3 sm:flex-row">
            <input
              id="url"
              placeholder="https://youtu.be/..."
              autocomplete="off"
              class="min-w-0 flex-1 rounded-2xl border border-white/10 bg-slate-950/80 px-4 py-3 text-slate-100 outline-none transition placeholder:text-slate-500 focus:border-sky-400 focus:ring-2 focus:ring-sky-400/20"
            >
            <button
              class="inline-flex items-center justify-center rounded-2xl bg-sky-500 px-5 py-3 text-sm font-bold text-slate-950 transition hover:bg-sky-400"
              onclick="submitJob()"
            >
              Add job
            </button>
          </div>
          <div id="submitStatus" class="mt-3 text-sm text-slate-400">Ready.</div>
        </section>

        <section class="rounded-3xl border border-white/10 bg-slate-900/75 p-5 shadow-glow backdrop-blur">
          <label for="outputDir" class="mb-3 block text-sm font-medium text-slate-300">Output directory</label>
          <div class="flex flex-col gap-3 sm:flex-row">
            <input
              id="outputDir"
              readonly
              value="__DEFAULT_OUTPUT_DIR__"
              class="min-w-0 flex-1 rounded-2xl border border-white/10 bg-slate-950/80 px-4 py-3 text-slate-100 outline-none transition focus:border-sky-400 focus:ring-2 focus:ring-sky-400/20"
            >
            <button
              class="inline-flex items-center justify-center rounded-2xl border border-white/10 bg-white/5 px-5 py-3 text-sm font-semibold text-slate-100 transition hover:border-emerald-300/40 hover:bg-emerald-400/10"
              onclick="setDefaultOutput()"
            >
              Use default
            </button>
          </div>
          <div class="mt-3 text-sm text-slate-400">
            Default:
            <code class="rounded bg-slate-950/80 px-1.5 py-0.5 text-sky-300">__DEFAULT_OUTPUT_DIR__</code>
          </div>
        </section>
      </div>

      <section class="mt-6 rounded-[28px] border border-white/10 bg-slate-900/75 p-4 shadow-glow backdrop-blur sm:p-6">
        <div class="mb-5 flex flex-col gap-2 border-b border-white/10 pb-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 class="text-xl font-bold text-white">Jobs</h2>
            <p class="mt-1 text-sm text-slate-400">Each download is shown as its own card with live progress, output path, and actions.</p>
          </div>
          <div class="text-xs uppercase tracking-[0.2em] text-slate-500">Auto refresh: 1.5s</div>
        </div>
        <div id="jobs" class="space-y-4"></div>
      </section>

      <div class="mt-6 flex flex-col gap-3 border-t border-white/10 pt-5 text-sm text-slate-400 sm:flex-row sm:items-center sm:justify-between">
        <div>API: <code class="rounded bg-slate-950/80 px-1.5 py-0.5 text-sky-300">/api/jobs</code>. TUI: <code class="rounded bg-slate-950/80 px-1.5 py-0.5 text-sky-300">python3 ytaria.py tui</code></div>
        <div>
          Developed and maintained by
          <a class="font-semibold text-sky-300 hover:text-sky-200" href="https://www.dawillygene.com/" target="_blank" rel="noreferrer">
            Elia William Mariki (dawillygene)
          </a>
        </div>
      </div>
    </div>
  </div>
  <script>
    const defaultOutput = __DEFAULT_OUTPUT_JSON__;
    function baseButtonClass() {{
      return 'inline-flex items-center justify-center rounded-2xl px-4 py-2.5 text-sm font-semibold transition';
    }}
    function secondaryButtonClass() {{
      return baseButtonClass() + ' border border-white/10 bg-white/5 text-slate-100 hover:border-sky-300/40 hover:bg-sky-400/10';
    }}
    function primaryButtonClass() {{
      return baseButtonClass() + ' bg-sky-500 text-slate-950 hover:bg-sky-400';
    }}
    function statusBadgeClass(status) {{
      const base = 'inline-flex items-center rounded-full px-3 py-1 text-xs font-bold uppercase tracking-[0.18em]';
      if (status === 'done') return base + ' bg-emerald-400/15 text-emerald-300';
      if (status === 'running') return base + ' bg-sky-400/15 text-sky-300';
      if (status === 'pending') return base + ' bg-amber-400/15 text-amber-300';
      if (status === 'paused') return base + ' bg-orange-400/15 text-orange-300';
      return base + ' bg-rose-400/15 text-rose-300';
    }}
    function progressBarClass(status) {{
      if (status === 'done') return 'bg-emerald-400';
      if (status === 'running') return 'bg-gradient-to-r from-sky-400 to-emerald-400';
      if (status === 'paused') return 'bg-amber-400';
      if (status === 'failed') return 'bg-rose-400';
      return 'bg-slate-500';
    }}
    function setDefaultOutput() {{
      document.getElementById('outputDir').value = defaultOutput;
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
        <div class="h-2.5 overflow-hidden rounded-full bg-white/10">
          <span class="block h-full rounded-full ${{progressBarClass(job.status)}}" style="width:${{clamped}}%"></span>
        </div>
        <div class="mt-2 text-sm text-slate-300">${{pct.toFixed(1)}}%${{detail ? ' • ' + detail : ''}}</div>
      `;
    }}
    function fmtActions(job) {{
      const cancel = `<button class="${{secondaryButtonClass()}}" onclick="cancelJob(${{job.id}})">Cancel</button>`;
      if (job.status === 'running') {{
        return `<button class="${{secondaryButtonClass()}}" onclick="pauseJob(${{job.id}})">Pause</button>${{cancel}}`;
      }}
      if (job.status === 'paused') {{
        return `<button class="${{primaryButtonClass()}}" onclick="resumeJob(${{job.id}})">Continue</button>${{cancel}}`;
      }}
      if (job.status === 'pending') {{
        return cancel;
      }}
      if (job.status === 'failed' || job.status === 'canceled') {{
        return `
          <button class="${{secondaryButtonClass()}}" onclick="retryJob(${{job.id}})">Retry</button>`;
      }}
      return '<span class="text-sm text-slate-500">No actions</span>';
    }}
    async function refreshJobs() {{
      const res = await fetch('/api/jobs');
      const data = await res.json();
      const jobsEl = document.getElementById('jobs');
      jobsEl.innerHTML = data.jobs.map(job => {{
        const hasTitle = job.title && job.title !== job.url;
        const err = job.status === 'failed' && job.error
          ? `<div class="mt-4 max-h-40 overflow-auto rounded-2xl border border-rose-400/30 bg-rose-400/10 px-4 py-3 font-mono text-xs leading-6 text-rose-200">${{esc(job.error)}}</div>` : '';
        return `
        <article class="rounded-[26px] border border-white/10 bg-slate-950/60 p-5 shadow-[0_10px_30px_rgba(0,0,0,0.22)]">
          <div class="flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
            <div class="min-w-0 flex-1">
              <div class="flex items-start gap-4">
                <span class="inline-flex h-11 min-w-11 shrink-0 items-center justify-center rounded-2xl border border-white/10 bg-white/5 px-3 text-sm font-bold text-slate-200">${{job.id}}</span>
                <div class="min-w-0 flex-1">
                  <h3 class="text-lg font-semibold leading-7 text-white break-words">${{hasTitle ? esc(job.title) : esc(job.url)}}</h3>
                  ${{hasTitle ? `<div class="mt-2 break-all text-sm leading-6 text-slate-400">${{esc(job.url)}}</div>` : ''}}
                </div>
              </div>
              ${{err}}
            </div>
            <div class="flex w-full flex-col gap-3 xl:max-w-xs">
              <div class="${{statusBadgeClass(job.status)}}">${{esc(job.status)}}</div>
              <div>${{fmtProgress(job)}}</div>
            </div>
          </div>

          <div class="mt-5 grid gap-4 lg:grid-cols-[minmax(0,1.5fr)_auto] lg:items-start">
            <div class="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
              <div class="text-xs font-bold uppercase tracking-[0.2em] text-slate-500">Output</div>
              <div class="mt-2 break-all text-sm leading-6 text-slate-200">${{esc(job.output_dir)}}</div>
              <div class="mt-2 break-all text-xs leading-6 text-slate-500">${{esc(job.destination || '')}}</div>
            </div>
            <div class="flex flex-wrap gap-3 lg:justify-end">${{fmtActions(job)}}</div>
          </div>
        </article>
      `;
      }}).join('') || `
        <div class="rounded-[26px] border border-dashed border-white/10 bg-slate-950/40 px-6 py-12 text-center">
          <div class="text-lg font-semibold text-white">No jobs yet</div>
          <div class="mt-2 text-sm text-slate-400">Paste a video URL above and queue your first download.</div>
        </div>
      `;
    }}
    async function submitJob() {{
      const url = document.getElementById('url').value.trim();
      const status = document.getElementById('submitStatus');
      if (!url) {{
        status.textContent = 'URL is required.';
        return;
      }}
      status.textContent = 'Submitting job...';
      const res = await fetch('/api/jobs', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{url}})
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
    running_pid = get_running_pid(Path(args.pid_file))
    if running_pid is not None and running_pid != os.getpid():
        raise SystemExit(
            f"ytaria-manager is already running with PID {running_pid} "
            f"(pid file: {args.pid_file})"
        )

    if args.background:
        daemonize()

    pid_path = Path(args.pid_file)
    write_pidfile(pid_path)
    atexit.register(remove_pidfile, pid_path)

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
        remove_pidfile(pid_path)


def start_service(args: argparse.Namespace) -> None:
    pid_path = Path(args.pid_file)
    running_pid, managed = get_service_pid(pid_path, args.host, args.port)
    if running_pid is not None:
        mode = "managed" if managed else "unmanaged"
        print(f"ytaria-manager is already running at http://{args.host}:{args.port}/ (PID {running_pid}, {mode}).")
        return

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--db",
        args.db,
        "--pid-file",
        args.pid_file,
        "serve",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    with open(os.devnull, "rb") as devnull_r, open(os.devnull, "ab") as devnull_w:
        subprocess.Popen(
            cmd,
            stdin=devnull_r,
            stdout=devnull_w,
            stderr=devnull_w,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.time() + 10.0
    while time.time() < deadline:
        started_pid, _ = get_service_pid(pid_path, args.host, args.port)
        if started_pid is not None:
            print(f"Started ytaria-manager at http://{args.host}:{args.port}/ (PID {started_pid}).")
            return
        time.sleep(0.1)
    raise SystemExit("ytaria-manager did not create its PID file; startup may have failed.")


def stop_service(args: argparse.Namespace) -> None:
    pid_path = Path(args.pid_file)
    pid, managed = get_service_pid(pid_path, args.host, args.port)
    if pid is None:
        print("ytaria-manager is not running.")
        remove_pidfile(pid_path)
        return
    if not stop_pid(pid):
        raise SystemExit(f"Failed to stop ytaria-manager cleanly (PID {pid}).")
    if managed:
        remove_pidfile(pid_path)
    print(f"Stopped ytaria-manager (PID {pid}).")


def status_service(args: argparse.Namespace) -> None:
    pid, managed = get_service_pid(Path(args.pid_file), args.host, args.port)
    if pid is None:
        print("ytaria-manager is stopped.")
        return
    mode = "managed" if managed else "unmanaged"
    extra = f", pid file: {args.pid_file}" if managed else ""
    print(f"ytaria-manager is running at http://{args.host}:{args.port}/ (PID {pid}, {mode}{extra}).")


def restart_service(args: argparse.Namespace) -> None:
    stop_service(args)
    start_service(args)


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
                        try:
                            url = validate_url(buffer.strip())
                            command = shlex.join(build_yt_dlp_command(url, DEFAULT_OUTPUT_DIR))
                            job_id = store.add_job(url, DEFAULT_OUTPUT_DIR, command)
                            message = f"Queued job #{job_id}."
                        except ValueError as exc:
                            message = f"Rejected: {exc}"
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
    try:
        url = validate_url(args.url)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")
    command = shlex.join(build_yt_dlp_command(url, output_dir))
    job_id = store.add_job(url, output_dir, command)
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
    parser.add_argument("--pid-file", default=str(DEFAULT_PID_PATH), help="PID file for the web service")

    sub = parser.add_subparsers(dest="cmd", required=True)

    start_p = sub.add_parser("start", help="Start the web UI in the background")
    start_p.add_argument("--host", default="127.0.0.1")
    start_p.add_argument("--port", type=int, default=8787)
    start_p.set_defaults(func=start_service)

    stop_p = sub.add_parser("stop", help="Stop the background web UI")
    stop_p.add_argument("--host", default="127.0.0.1")
    stop_p.add_argument("--port", type=int, default=8787)
    stop_p.set_defaults(func=stop_service)

    restart_p = sub.add_parser("restart", help="Restart the background web UI")
    restart_p.add_argument("--host", default="127.0.0.1")
    restart_p.add_argument("--port", type=int, default=8787)
    restart_p.set_defaults(func=restart_service)

    status_p = sub.add_parser("status", help="Show whether the web UI is running")
    status_p.add_argument("--host", default="127.0.0.1")
    status_p.add_argument("--port", type=int, default=8787)
    status_p.set_defaults(func=status_service)

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
