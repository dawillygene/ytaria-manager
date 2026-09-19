"""Job orchestration: creation, user controls, worker claiming/leases and completion.

Concurrency model
-----------------
* Every status change locks the job row (``SELECT ... FOR UPDATE``), validates the move against
  ``states.TRANSITIONS`` and records a ``JobEvent`` in the same transaction.
* Workers hold a *lease* (``lease_token`` + ``lease_expires_at``). Every worker write is fenced on the
  token, so a worker that lost its lease (crash recovery already re-queued the job) cannot overwrite
  newer state.
* Claiming is serialised with a transaction-scoped advisory lock so global and per-user concurrency
  limits are exact, and a duplicate task delivery simply finds the job already ``running`` and exits.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Inspection, Job, JobEvent, User
from ..security import utcnow
from ..states import ACTIVE, ACTIVE_VALUES, DELETABLE, LEASED, RETRYABLE, JobStatus, can_transition
from .errors import Conflict, NotFound, ServiceError
from .engine import Failure, get_selection

S = JobStatus
CLAIM_LOCK_KEY = 0x59544152  # "YTAR"
DEFER_SECONDS = 5


class InvalidTransition(RuntimeError):
    """Programming error: code attempted a move that is not in the state machine."""


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def add_event(session: Session, job: Job, kind: str, message: str = "", from_status: str | None = None, to_status: str | None = None) -> None:
    session.add(JobEvent(job_id=job.id, user_id=job.user_id, kind=kind, message=message[:300], from_status=from_status, to_status=to_status))


def _lock_job(session: Session, job_id: uuid.UUID, user_id: uuid.UUID | None = None) -> Job | None:
    stmt = select(Job).where(Job.id == job_id)
    if user_id is not None:
        stmt = stmt.where(Job.user_id == user_id, Job.deleted_at.is_(None))
    stmt = stmt.with_for_update()
    return session.execute(stmt.execution_options(populate_existing=True)).scalar_one_or_none()


def move(session: Session, job: Job, dst: JobStatus, message: str = "", **values: Any) -> None:
    """Apply a validated transition to an already-locked job."""
    src = JobStatus(job.status)
    if src == dst:
        raise InvalidTransition(f"{src} -> {dst} is a no-op")
    if not can_transition(src, dst):
        raise InvalidTransition(f"illegal transition {src} -> {dst}")
    job.status = dst.value
    for key, value in values.items():
        setattr(job, key, value)
    add_event(session, job, "state", message, src.value, dst.value)
    session.flush()


def backoff_seconds(settings: Settings, attempt: int) -> int:
    base = settings.retry_backoff_base_seconds * (2 ** max(0, attempt - 1))
    capped = min(base, settings.retry_backoff_cap_seconds)
    return int(capped * random.uniform(0.75, 1.25))


def user_disk_usage(session: Session, user_id: uuid.UUID) -> int:
    return int(session.scalar(select(func.coalesce(func.sum(Job.disk_bytes), 0)).where(Job.user_id == user_id)) or 0)


def user_quota(settings: Settings, user: User) -> int:
    return user.quota_bytes if user.quota_bytes is not None else settings.user_quota_bytes


def active_count(session: Session, user_id: uuid.UUID) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(Job).where(Job.user_id == user_id, Job.status.in_(ACTIVE_VALUES), Job.deleted_at.is_(None))
        )
        or 0
    )


def _check_capacity(session: Session, settings: Settings, user: User, extra_bytes: int | None) -> None:
    if active_count(session, user.id) >= settings.max_active_jobs_per_user:
        raise ServiceError("queue_limit", f"You can have at most {settings.max_active_jobs_per_user} active downloads. Wait for one to finish or cancel one.", 429)
    if user_disk_usage(session, user.id) + (extra_bytes or 0) > user_quota(settings, user):
        raise ServiceError("quota_exceeded", "Not enough storage left. Delete some completed downloads first.", 409)


# ---------------------------------------------------------------------------------------------
# User-facing operations
# ---------------------------------------------------------------------------------------------


def get_job(session: Session, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    """Fetch a job owned by ``user_id``. Other users' jobs are indistinguishable from missing ones."""
    job = session.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if job is None:
        raise NotFound("Download not found.")
    return job


def create_job(
    session: Session,
    settings: Settings,
    user: User,
    inspection_id: uuid.UUID,
    selection_id: str,
    idempotency_key: str | None,
) -> tuple[Job, bool]:
    """Create a queued job. Returns ``(job, created)``; ``created`` is False for a duplicate/replay."""
    if idempotency_key:
        existing = session.execute(select(Job).where(Job.user_id == user.id, Job.idempotency_key == idempotency_key)).scalar_one_or_none()
        if existing:
            return existing, False
    # Serialise this user's submissions so limit checks and dedupe cannot race.
    session.execute(select(User.id).where(User.id == user.id).with_for_update())

    insp = session.execute(select(Inspection).where(Inspection.id == inspection_id, Inspection.user_id == user.id)).scalar_one_or_none()
    if insp is None:
        raise NotFound("Inspection not found. Inspect the link again.")
    if insp.status != "succeeded" or not insp.result:
        raise Conflict("inspection_not_ready", "This link has not been inspected successfully.")
    if insp.expires_at < utcnow():
        raise Conflict("inspection_expired", "The inspection expired. Inspect the link again.")
    selection = get_selection(selection_id)
    option = next((o for o in insp.result["options"] if o["id"] == selection_id), None)
    if selection is None or option is None:
        raise ServiceError("invalid_selection", "That quality is not available for this link.", 422)

    duplicate = session.execute(
        select(Job).where(Job.user_id == user.id, Job.url_hash == insp.url_hash, Job.selection == selection_id,
                          Job.status.in_(ACTIVE_VALUES), Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if duplicate:
        return duplicate, False

    expected = option.get("estimated_bytes")
    _check_capacity(session, settings, user, expected)
    if expected and expected > settings.max_file_bytes:
        raise ServiceError("too_large", "That download is larger than the allowed file size.", 413)

    from urllib.parse import urlsplit

    job = Job(
        user_id=user.id,
        url=insp.url,
        url_hash=insp.url_hash,
        host=urlsplit(insp.url).hostname or "",
        selection=selection_id,
        idempotency_key=idempotency_key,
        title=insp.result.get("title"),
        duration_seconds=insp.result.get("duration_seconds"),
        expected_bytes=expected,
        status=S.QUEUED.value,
        max_attempts=settings.max_attempts,
        next_attempt_at=utcnow(),
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent identical request won the unique index; return that one.
        session.rollback()
        winner = session.execute(
            select(Job).where(Job.user_id == user.id, Job.url_hash == insp.url_hash, Job.selection == selection_id,
                              Job.status.in_(ACTIVE_VALUES), Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if winner is None and idempotency_key:
            winner = session.execute(select(Job).where(Job.user_id == user.id, Job.idempotency_key == idempotency_key)).scalar_one_or_none()
        if winner is None:
            raise
        return winner, False
    add_event(session, job, "state", "Queued", None, S.QUEUED.value)
    return job, True


def pause_job(session: Session, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = _lock_job(session, job_id, user_id)
    if job is None:
        raise NotFound("Download not found.")
    status = JobStatus(job.status)
    if status in (S.PAUSED, S.PAUSING):
        return job  # idempotent
    if status == S.QUEUED:
        move(session, job, S.PAUSED, "Paused before start", enqueued_at=None)
    elif status == S.RUNNING:
        move(session, job, S.PAUSING, "Pause requested")
    else:
        raise Conflict("not_pausable", f"A {status.value} download can't be paused.")
    return job


def resume_job(session: Session, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = _lock_job(session, job_id, user_id)
    if job is None:
        raise NotFound("Download not found.")
    status = JobStatus(job.status)
    if status in (S.QUEUED, S.RUNNING):
        return job  # idempotent
    if status == S.PAUSING:
        raise Conflict("pause_in_progress", "The download is still pausing. Try again in a moment.")
    if status != S.PAUSED:
        raise Conflict("not_resumable", f"A {status.value} download can't be resumed.")
    move(session, job, S.QUEUED, "Resumed", next_attempt_at=utcnow(), enqueued_at=None, speed_bps=None, eta_seconds=None)
    return job


def cancel_job(session: Session, user_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    job = _lock_job(session, job_id, user_id)
    if job is None:
        raise NotFound("Download not found.")
    status = JobStatus(job.status)
    if status in (S.CANCELING, S.CANCELED):
        return job  # idempotent
    if status in (S.QUEUED, S.PAUSED):
        move(session, job, S.CANCELED, "Canceled", finished_at=utcnow(), enqueued_at=None, speed_bps=None, eta_seconds=None,
             error_code=None, error_message=None)
    elif status in (S.RUNNING, S.PAUSING):
        move(session, job, S.CANCELING, "Cancel requested")
    else:
        raise Conflict("not_cancelable", f"A {status.value} download can't be canceled.")
    return job


def retry_job(session: Session, settings: Settings, user: User, job_id: uuid.UUID) -> Job:
    session.execute(select(User.id).where(User.id == user.id).with_for_update())
    job = _lock_job(session, job_id, user.id)
    if job is None:
        raise NotFound("Download not found.")
    status = JobStatus(job.status)
    if status in (S.QUEUED, S.RUNNING):
        return job  # idempotent
    if status not in RETRYABLE:
        raise Conflict("not_retryable", f"A {status.value} download can't be retried.")
    _check_capacity(session, settings, user, job.expected_bytes)
    try:
        move(session, job, S.QUEUED, "Retry requested", attempts=0, progress_percent=0.0, downloaded_bytes=0, total_bytes=None,
             speed_bps=None, eta_seconds=None, stage=None, error_code=None, error_message=None, finished_at=None,
             next_attempt_at=utcnow(), enqueued_at=None, expires_at=None, file_relpath=None, file_name=None, file_size=None,
             file_mime=None, files_purged_at=None if status == S.FAILED else job.files_purged_at)
    except IntegrityError:
        session.rollback()
        raise Conflict("duplicate_active", "The same download is already active.") from None
    return job


def delete_job(session: Session, user_id: uuid.UUID, job_id: uuid.UUID) -> None:
    job = _lock_job(session, job_id, user_id)
    if job is None:
        raise NotFound("Download not found.")
    if JobStatus(job.status) not in DELETABLE:
        raise Conflict("not_deletable", "Cancel the download before removing it.")
    job.deleted_at = utcnow()
    job.expires_at = utcnow()
    add_event(session, job, "info", "Removed by user")
    session.flush()


# ---------------------------------------------------------------------------------------------
# Worker-side lifecycle
# ---------------------------------------------------------------------------------------------


@dataclass
class Claim:
    outcome: str  # claimed | skipped | deferred
    job: Job | None = None
    token: uuid.UUID | None = None


def claim_job(session: Session, settings: Settings, job_id: uuid.UUID, owner: str, free_disk: int | None = None) -> Claim:
    """Atomically move a queued job to running, respecting global/per-user concurrency."""
    session.execute(select(func.pg_advisory_xact_lock(CLAIM_LOCK_KEY)))
    job = _lock_job(session, job_id)
    now = utcnow()
    if job is None or job.status != S.QUEUED.value or job.deleted_at is not None:
        return Claim("skipped", job)  # duplicate delivery, canceled/paused meanwhile, or already running
    if job.next_attempt_at > now:
        job.enqueued_at = None  # the reconciler re-dispatches once the backoff has elapsed
        return Claim("deferred", job)

    live = (Job.status.in_([s.value for s in LEASED])) & (Job.lease_expires_at > now)
    running_total = session.scalar(select(func.count()).select_from(Job).where(live)) or 0
    running_user = session.scalar(select(func.count()).select_from(Job).where(live, Job.user_id == job.user_id)) or 0
    disk_low = free_disk is not None and free_disk < settings.min_free_disk_bytes
    if running_total >= settings.max_concurrent_jobs_global or running_user >= settings.max_concurrent_jobs_per_user or disk_low:
        job.enqueued_at = None
        job.next_attempt_at = now + timedelta(seconds=DEFER_SECONDS)
        return Claim("deferred", job)

    token = uuid.uuid4()
    move(session, job, S.RUNNING, f"Attempt {job.attempts + 1} started",
         attempts=job.attempts + 1, lease_token=token, lease_owner=owner[:120],
         lease_expires_at=now + timedelta(seconds=settings.lease_seconds), started_at=job.started_at or now,
         stage="starting", error_code=None, error_message=None, enqueued_at=job.enqueued_at or now)
    return Claim("claimed", job, token)


def heartbeat(session: Session, settings: Settings, job_id: uuid.UUID, token: uuid.UUID) -> JobStatus | None:
    """Extend the lease. Returns the current status, or ``None`` if this worker no longer owns the job."""
    row = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_token == token, Job.status.in_([s.value for s in LEASED]))
        .values(lease_expires_at=utcnow() + timedelta(seconds=settings.lease_seconds))
        .returning(Job.status)
    ).first()
    return JobStatus(row[0]) if row else None


def record_progress(session: Session, job_id: uuid.UUID, token: uuid.UUID, **values: Any) -> bool:
    res = session.execute(
        update(Job).where(Job.id == job_id, Job.lease_token == token, Job.status.in_([s.value for s in LEASED])).values(**values)
    )
    return bool(res.rowcount)


def complete_job(
    session: Session, settings: Settings, job_id: uuid.UUID, token: uuid.UUID, *, relpath: str, file_name: str, size: int, mime: str,
) -> bool:
    """Mark completed. Returns False if the job was canceled/reassigned; the caller must then delete the file."""
    job = _lock_job(session, job_id)
    if job is None or job.lease_token != token or job.status not in (S.RUNNING.value, S.PAUSING.value):
        return False  # includes CANCELING: a cancel always wins over a racing completion
    now = utcnow()
    move(session, job, S.COMPLETED, "Ready on server", progress_percent=100.0, stage=None, speed_bps=None, eta_seconds=None,
         downloaded_bytes=size, total_bytes=size, file_relpath=relpath, file_name=file_name, file_size=size, file_mime=mime,
         disk_bytes=size, finished_at=now, expires_at=now + timedelta(hours=settings.completed_retention_hours),
         lease_token=None, lease_owner=None, lease_expires_at=None, error_code=None, error_message=None)
    return True


def fail_or_retry(session: Session, settings: Settings, job_id: uuid.UUID, token: uuid.UUID, failure: Failure, debug_tail: str = "") -> str:
    """Record a failed attempt. Returns ``retry`` | ``failed`` | ``canceled`` | ``paused`` | ``lost``."""
    job = _lock_job(session, job_id)
    if job is None or job.lease_token != token:
        return "lost"
    status = JobStatus(job.status)
    clear = dict(lease_token=None, lease_owner=None, lease_expires_at=None, speed_bps=None, eta_seconds=None, stage=None)
    if status == S.CANCELING:
        move(session, job, S.CANCELED, "Canceled", finished_at=utcnow(), **clear)
        return "canceled"
    if status not in (S.RUNNING, S.PAUSING):
        return "lost"
    if status == S.PAUSING:
        move(session, job, S.PAUSED, "Paused", attempts=max(0, job.attempts - 1), **clear)
        return "paused"
    if failure.retryable and job.attempts < job.max_attempts:
        delay = backoff_seconds(settings, job.attempts)
        move(session, job, S.QUEUED, f"{failure.message} Retrying in {delay}s (attempt {job.attempts + 1} of {job.max_attempts}).",
             next_attempt_at=utcnow() + timedelta(seconds=delay), enqueued_at=None, error_code=failure.code,
             error_message=failure.message, debug_tail=debug_tail[-4000:], **clear)
        return "retry"
    move(session, job, S.FAILED, failure.message, finished_at=utcnow(), error_code=failure.code, error_message=failure.message,
         debug_tail=debug_tail[-4000:], **clear)
    return "failed"


def finish_stop(session: Session, job_id: uuid.UUID, token: uuid.UUID, reason: str) -> str:
    """Finalise a worker-initiated stop for ``pause`` or ``cancel``. Returns the resulting outcome."""
    job = _lock_job(session, job_id)
    if job is None or job.lease_token != token:
        return "lost"
    status = JobStatus(job.status)
    clear = dict(lease_token=None, lease_owner=None, lease_expires_at=None, speed_bps=None, eta_seconds=None, stage=None)
    if status == S.PAUSING or (reason == "pause" and status == S.RUNNING):
        if status == S.RUNNING:
            move(session, job, S.PAUSING, "Pause requested")
        move(session, job, S.PAUSED, "Paused (partial data kept)", attempts=max(0, job.attempts - 1), **clear)
        return "paused"
    if status == S.CANCELING:
        move(session, job, S.CANCELED, "Canceled", finished_at=utcnow(), **clear)
        return "canceled"
    return "lost"


# ---------------------------------------------------------------------------------------------
# Serialisation helpers used by the API
# ---------------------------------------------------------------------------------------------


def list_jobs(
    session: Session, user_id: uuid.UUID, *, scope: str, page: int, page_size: int
) -> tuple[list[Job], int]:
    base = select(Job).where(Job.user_id == user_id, Job.deleted_at.is_(None))
    if scope == "active":
        base = base.where(Job.status.in_(ACTIVE_VALUES))
    elif scope == "history":
        base = base.where(Job.status.notin_(ACTIVE_VALUES))
    elif scope == "ready":  # finished on the server and still downloadable
        base = base.where(Job.status == JobStatus.COMPLETED.value, Job.file_relpath.is_not(None))
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.execute(base.order_by(Job.created_at.desc(), Job.id).limit(page_size).offset((page - 1) * page_size)).scalars().all()
    return list(rows), int(total)


def list_events(session: Session, user_id: uuid.UUID, job_id: uuid.UUID, limit: int = 100) -> list[JobEvent]:
    get_job(session, user_id, job_id)
    return list(
        session.execute(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.user_id == user_id).order_by(JobEvent.id.desc()).limit(limit)).scalars()
    )


def mark_purged(session: Session, job_id: uuid.UUID) -> None:
    """Bookkeeping after a job's files were deleted (unfenced: the job is already terminal)."""
    session.execute(update(Job).where(Job.id == job_id).values(disk_bytes=0, files_purged_at=utcnow()))
