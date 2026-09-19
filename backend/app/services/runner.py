"""Run a download subprocess under a pty with control polling and guaranteed group termination."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .engine import ANSI_RE

TAIL_LINES = 30


@dataclass
class RunResult:
    exit_code: int | None
    # exit | pause | cancel | timeout | lease_lost | limit
    reason: str
    tail: list[str] = field(default_factory=list)


def kill_process_group(process: "subprocess.Popen[bytes]", grace: float = 10.0) -> None:
    """SIGTERM the whole group (yt-dlp *and* its aria2c/ffmpeg children), then SIGKILL after ``grace``."""
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    def send(sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                process.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    send(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if process.poll() is not None and not _group_alive(pgid):
            return
        time.sleep(0.1)
    send(signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def _group_alive(pgid: int | None) -> bool:
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_controlled(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: Path | None,
    on_line: Callable[[str], None],
    control: Callable[[], str | None],
    max_runtime: float,
    grace: float = 10.0,
    poll_interval: float = 1.0,
) -> RunResult:
    """Run ``cmd`` and return why it stopped.

    ``control`` is called about once per ``poll_interval``; it may return ``"pause"``, ``"cancel"``,
    ``"lease_lost"`` or ``"limit"`` to request termination. The worker uses it to heartbeat and to
    observe DB-side control requests. The whole process group is always terminated before returning.
    """
    master_fd, slave_fd = pty.openpty()
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 220, 0, 0))
    except OSError:
        pass
    full_cmd = list(cmd)
    setpriv = shutil.which("setpriv")
    if setpriv:  # child dies with the worker process if the worker is SIGKILLed
        full_cmd = [setpriv, "--pdeathsig", "KILL", "--", *full_cmd]
    process = subprocess.Popen(
        full_cmd,
        stdin=subprocess.DEVNULL,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        cwd=str(cwd) if cwd else None,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    buf = b""
    started = time.monotonic()
    next_control = started
    stop_reason: str | None = None
    try:
        while True:
            now = time.monotonic()
            if now >= next_control:
                next_control = now + poll_interval
                requested = control()
                if requested:
                    stop_reason = requested
                    break
            if now - started > max_runtime:
                stop_reason = "timeout"
                break
            ready, _, _ = select.select([master_fd], [], [], min(0.5, poll_interval))
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buf = (buf + chunk).replace(b"\r", b"\n")
                *complete, buf = buf.split(b"\n")
                for raw in complete:
                    line = ANSI_RE.sub("", raw.decode("utf-8", "replace")).rstrip()
                    if not line:
                        continue
                    if not line.startswith("[#"):
                        tail.append(line[:500])
                    on_line(line)
            elif process.poll() is not None:
                # No more output and the leader exited; drain anything left, then stop.
                break
    finally:
        if stop_reason is not None or process.poll() is None or _group_alive_safe(process):
            kill_process_group(process, grace)
        try:
            os.close(master_fd)
        except OSError:
            pass
    if stop_reason is not None:
        return RunResult(None, stop_reason, list(tail))
    return RunResult(process.wait(), "exit", list(tail))


def _group_alive_safe(process: "subprocess.Popen[bytes]") -> bool:
    try:
        return _group_alive(os.getpgid(process.pid))
    except (ProcessLookupError, OSError):
        return False
