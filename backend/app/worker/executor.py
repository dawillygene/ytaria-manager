"""Executes one download attempt: claim -> run subprocess -> finalise. Contains no Celery imports."""

from __future__ import annotations

import logging
import os
import shutil
import socket
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..config import Settings
from ..db import session_scope
from ..logging_utils import redact
from ..models import Job, User
from ..services import jobs as jobsvc
from ..services import storage
from ..services.engine import (
    Failure,
    Selection,
    build_download_command,
    classify_failure,
    get_selection,
    parse_line,
    safe_env,
)
from ..services.runner import RunResult, run_controlled
from ..services.urlpolicy import UrlRejected, validate_source_url
from ..states import JobStatus

log = logging.getLogger("ytaria.worker")

CommandFactory = Callable[[Settings, str, Selection, Path, Path], list[str]]

_TIMEOUT = Failure("timeout", "The download took too long and was stopped.", False)
_NO_OUTPUT = Failure("no_output", "The download finished but no media file was produced.", False)
_BAD_TYPE = Failure("unsupported_file_type", "The downloaded file type is not supported for transfer.", False)
_QUOTA = Failure("quota_exceeded", "The download exceeded your storage quota and was stopped.", False)
_TOO_LARGE = Failure("too_large", "The download exceeded the allowed file size and was stopped.", False)
_POLICY = Failure("url_rejected", "This link is no longer allowed.", False)
_CRASH = Failure("failed", "The download failed.", True)
# Failures after which partial data is useless and is freed immediately instead of after the retention window.
_FREE_PARTIALS = {"no_output", "unsupported_file_type", "too_large", "quota_exceeded", "timeout", "unsupported", "restricted", "unavailable", "drm"}


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _default_command(settings: Settings, url: str, selection: Selection, work: Path, cache: Path) -> list[str]:
    return build_download_command(settings, url, selection, work, cache)


def execute_download(
    settings: Settings, job_id: uuid.UUID, owner: str | None = None, command_factory: CommandFactory | None = None
) -> str:
    """Run one attempt. Returns ``skipped|deferred|completed|retry|failed|paused|canceled|lost``."""
    owner = owner or worker_id()
    with session_scope() as s:
        claim = jobsvc.claim_job(s, settings, job_id, owner, storage.free_disk_bytes(settings))
        if claim.outcome != "claimed":
            return claim.outcome
        job = claim.job
        assert job is not None and claim.token is not None
        token, url, selection_id, user_id = claim.token, job.url, job.selection, job.user_id
    log.info("claimed", extra={"job_id": str(job_id), "worker": owner})

    try:
        validate_source_url(url, allowed_hosts=settings.allowed_source_hosts, allowed_ports=settings.allowed_ports)
    except UrlRejected:
        return _fail(settings, job_id, token, _POLICY, [])
    selection = get_selection(selection_id)
    if selection is None:
        return _fail(settings, job_id, token, Failure("invalid_selection", "The selected quality is not available.", False), [])
    if settings.in_production and not settings.egress_proxy_url:
        return _fail(settings, job_id, token, Failure("egress_unconfigured", "The server is not configured for downloads.", False), [])

    work = storage.ensure_job_dirs(settings, job_id)
    scratch = settings.scratch_root.resolve() / "jobs" / str(job_id)
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        cmd = (command_factory or _default_command)(settings, url, selection, work, scratch / "cache")
        state: dict = {"percent": 0.0, "written": 0.0, "hb": 0.0, "pending": {}, "limit": None}

        def flush(force: bool = False) -> None:
            if not state["pending"] or (not force and time.monotonic() - state["written"] < 1.0):
                return
            with session_scope() as s:
                jobsvc.record_progress(s, job_id, token, **state["pending"])
            state["pending"], state["written"] = {}, time.monotonic()

        def on_line(line: str) -> None:
            prog = parse_line(line)
            if prog is None:
                return
            pending: dict = state["pending"]
            if prog.percent is not None:
                # Monotonic: video and audio streams each count 0-100; 100 is only set on completion.
                state["percent"] = max(state["percent"], min(prog.percent, 99.0))
                pending["progress_percent"] = state["percent"]
            if prog.downloaded is not None:
                pending["downloaded_bytes"] = prog.downloaded
            if prog.total is not None:
                pending["total_bytes"] = prog.total
            if prog.percent is not None or prog.speed is not None:
                pending["speed_bps"], pending["eta_seconds"] = prog.speed, prog.eta
            if prog.stage:
                pending["stage"] = prog.stage
            flush()

        def control() -> str | None:
            if time.monotonic() - state["hb"] < settings.heartbeat_seconds:
                return None
            state["hb"] = time.monotonic()
            size = storage.dir_size(storage.job_dir(settings, job_id))
            with session_scope() as s:
                status = jobsvc.heartbeat(s, settings, job_id, token)
                if status is None:
                    return "lease_lost"
                jobsvc.record_progress(s, job_id, token, disk_bytes=size)
                if status == JobStatus.CANCELING:
                    return "cancel"
                if status == JobStatus.PAUSING:
                    return "pause"
                user = s.get(User, user_id)
                over_quota = user is not None and jobsvc.user_disk_usage(s, user_id) > jobsvc.user_quota(settings, user)
            if size > settings.max_file_bytes * 2 + 64 * 1024 * 1024:  # separate streams + merged output
                state["limit"] = _TOO_LARGE
                return "limit"
            if over_quota:
                state["limit"] = _QUOTA
                return "limit"
            return None

        try:
            result = run_controlled(cmd, env=safe_env(settings, scratch), cwd=work, on_line=on_line, control=control,
                                    max_runtime=settings.job_max_runtime_seconds, grace=settings.kill_grace_seconds,
                                    poll_interval=min(1.0, max(0.1, float(settings.heartbeat_seconds))))
            flush(force=True)
        except Exception:
            log.exception("runner crashed", extra={"job_id": str(job_id)})
            return _after_failure(settings, job_id, token, _CRASH, ["runner crashed"])
        return _finalize(settings, job_id, token, result, state)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _fail(settings: Settings, job_id: uuid.UUID, token: uuid.UUID, failure: Failure, tail: list[str]) -> str:
    with session_scope() as s:
        return jobsvc.fail_or_retry(s, settings, job_id, token, failure, redact("\n".join(tail)))


def _after_failure(settings: Settings, job_id: uuid.UUID, token: uuid.UUID, failure: Failure, tail: list[str]) -> str:
    outcome = _fail(settings, job_id, token, failure, tail)
    if outcome == "canceled":
        _purge(settings, job_id)
    elif outcome == "failed" and failure.code in _FREE_PARTIALS:
        _purge(settings, job_id)
    return outcome


def _purge(settings: Settings, job_id: uuid.UUID) -> None:
    storage.remove_job_files(settings, job_id)
    with session_scope() as s:
        jobsvc.mark_purged(s, job_id)


def _finalize(settings: Settings, job_id: uuid.UUID, token: uuid.UUID, result: RunResult, state: dict) -> str:
    reason = result.reason
    if reason == "lease_lost":
        log.warning("lease lost; leaving state to the reconciler", extra={"job_id": str(job_id)})
        return "lost"
    if reason in ("pause", "cancel"):
        with session_scope() as s:
            outcome = jobsvc.finish_stop(s, job_id, token, reason)
        if outcome == "canceled":
            _purge(settings, job_id)  # canceled jobs never keep partial data
        return outcome  # pause keeps the work directory so the next attempt can resume
    if reason == "timeout":
        return _after_failure(settings, job_id, token, _TIMEOUT, result.tail)
    if reason == "limit":
        return _after_failure(settings, job_id, token, state["limit"] or _TOO_LARGE, result.tail)
    if result.exit_code != 0:
        return _after_failure(settings, job_id, token, classify_failure(result.tail), result.tail)

    found = storage.find_output_file(storage.work_dir(settings, job_id))
    if found is None:
        unsupported = storage.has_unsupported_output(storage.work_dir(settings, job_id))
        return _after_failure(settings, job_id, token, _BAD_TYPE if unsupported else _NO_OUTPUT, result.tail)
    with session_scope() as s:
        job = s.get(Job, job_id)
        title = job.title if job else None
    relpath = storage.finalize_file(settings, job_id, found)
    with session_scope() as s:
        ok = jobsvc.complete_job(s, settings, job_id, token, relpath=relpath,
                                 file_name=storage.sanitize_filename(title, found.ext), size=found.size,
                                 mime=storage.mime_for_ext(found.ext))
    if ok:
        storage.remove_work_dir(settings, job_id)
        return "completed"
    # A cancel (or lease loss) beat the completion: the file must not be published.
    with session_scope() as s:
        outcome = jobsvc.finish_stop(s, job_id, token, "cancel")
    if outcome == "canceled":
        _purge(settings, job_id)
    else:
        storage.remove_tree(storage.final_dir(settings, job_id), storage.media_root(settings))
    return outcome
