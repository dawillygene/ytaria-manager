from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update

from app.db import session_scope
from app.models import Job
from app.security import utcnow
from app.services import jobs as jobsvc
from app.services import storage

FAKE = str(Path(__file__).with_name("fake_ytdlp.py"))


def factory(mode: str):
    def _f(settings, url, selection, work, cache):
        # the worker scrubs the environment, so test knobs travel as arguments
        return [sys.executable, FAKE, mode, str(work), os.environ.get("FAKE_PIDFILE", ""), os.environ.get("FAKE_DELAY", "1.5")]

    return _f


def get(job_id) -> Job:
    with session_scope() as s:
        return s.get(Job, uuid.UUID(str(job_id)))


def make_due(job_id) -> None:
    with session_scope() as s:
        s.execute(update(Job).where(Job.id == uuid.UUID(str(job_id))).values(next_attempt_at=utcnow() - timedelta(seconds=1)))


def complete_with_file(settings, job_id, data: bytes = b"0123456789" * 100, ext: str = "mp4", title: str = "My Clip") -> None:
    """Drive a job to `completed` with a real stored file, through the same service calls the worker uses."""
    job_id = uuid.UUID(str(job_id))
    with session_scope() as s:
        claim = jobsvc.claim_job(s, settings, job_id, "test-worker")
        assert claim.outcome == "claimed", claim.outcome
        token = claim.token
    work = storage.ensure_job_dirs(settings, job_id)
    (work / f"vid.{ext}").write_bytes(data)
    found = storage.find_output_file(work)
    rel = storage.finalize_file(settings, job_id, found)
    with session_scope() as s:
        assert jobsvc.complete_job(s, settings, job_id, token, relpath=rel, file_name=storage.sanitize_filename(title, ext),
                                   size=len(data), mime=storage.mime_for_ext(ext))


def wait_for(predicate, timeout=15.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombies count as dead
        return Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0] != "Z"
    except OSError:
        return False
