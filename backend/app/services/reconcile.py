"""Periodic reconciliation and cleanup. Every function is idempotent and safe to run concurrently."""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import DownloadTicket, Inspection, Job, RefreshToken, Transfer
from ..security import utcnow
from ..states import LEASED, JobStatus
from . import storage
from .engine import Failure
from .jobs import _lock_job, add_event, backoff_seconds, move

S = JobStatus
log = logging.getLogger("ytaria.reconcile")
RECONCILE_LOCK = 0x5952434F
CLEANUP_LOCK = 0x59434C4E


def try_advisory_lock(session: Session, key: int) -> bool:
    """Session-level try-lock so overlapping scheduler runs never both act."""
    return bool(session.scalar(select(func.pg_try_advisory_xact_lock(key))))


def recover_expired_leases(session: Session, settings: Settings) -> int:
    """Jobs whose worker vanished (crash, OOM, container restart) are re-queued, failed or finalised."""
    now = utcnow()
    ids = session.scalars(
        select(Job.id).where(Job.status.in_([s.value for s in LEASED]), Job.lease_expires_at < now).limit(100)
    ).all()
    handled = 0
    clear = dict(lease_token=None, lease_owner=None, lease_expires_at=None, speed_bps=None, eta_seconds=None, stage=None)
    for job_id in ids:
        job = _lock_job(session, job_id)
        if job is None or job.status not in [s.value for s in LEASED] or job.lease_expires_at is None or job.lease_expires_at >= now:
            continue  # heartbeat arrived meanwhile
        status = JobStatus(job.status)
        if status == S.CANCELING:
            move(session, job, S.CANCELED, "Canceled (worker was lost)", finished_at=now, **clear)
        elif status == S.PAUSING:
            move(session, job, S.PAUSED, "Paused (worker was lost)", attempts=max(0, job.attempts - 1), **clear)
        elif job.attempts < job.max_attempts:
            delay = backoff_seconds(settings, job.attempts)
            move(session, job, S.QUEUED, f"Worker stopped responding. Retrying in {delay}s.",
                 next_attempt_at=now + timedelta(seconds=delay), enqueued_at=None, error_code="worker_lost",
                 error_message="The worker stopped responding.", **clear)
        else:
            move(session, job, S.FAILED, "Worker stopped responding too many times.", finished_at=now,
                 error_code="worker_lost", error_message="The download could not be completed after several attempts.", **clear)
        handled += 1
    return handled


def redispatch_queued(session: Session, settings: Settings, dispatch_download: Callable[[uuid.UUID], None],
                      dispatch_inspection: Callable[[uuid.UUID], None]) -> int:
    """Re-publish jobs/inspections that were committed but never (or no longer) enqueued."""
    now = utcnow()
    stale = now - timedelta(seconds=settings.dispatch_stale_seconds)
    count = 0
    jobs = session.scalars(
        select(Job).where(
            Job.status == S.QUEUED.value, Job.deleted_at.is_(None), Job.next_attempt_at <= now,
            or_(Job.enqueued_at.is_(None), Job.enqueued_at < stale),
        ).order_by(Job.next_attempt_at).limit(100).with_for_update(skip_locked=True)
    ).all()
    for job in jobs:
        try:
            dispatch_download(job.id)
        except Exception:
            log.warning("dispatch failed for job %s; will retry", job.id)
            continue
        job.enqueued_at = now
        count += 1
    inspections = session.scalars(
        select(Inspection).where(Inspection.status == "pending", Inspection.expires_at > now,
                                 or_(Inspection.enqueued_at.is_(None), Inspection.enqueued_at < now - timedelta(seconds=120)))
        .limit(100).with_for_update(skip_locked=True)
    ).all()
    for insp in inspections:
        try:
            dispatch_inspection(insp.id)
        except Exception:
            log.warning("dispatch failed for inspection %s; will retry", insp.id)
            continue
        insp.enqueued_at = now
        count += 1
    return count


def _has_active_transfer(session: Session, settings: Settings, job_id: uuid.UUID) -> bool:
    cutoff = utcnow() - timedelta(seconds=settings.transfer_active_window_seconds)
    return bool(session.scalar(
        select(func.count()).select_from(Transfer).where(Transfer.job_id == job_id, Transfer.server_completed_at.is_(None),
                                                         Transfer.last_seen_at > cutoff)))


def _purge(session: Session, settings: Settings, job: Job, *, work_only: bool = False) -> None:
    try:
        if work_only:
            storage.remove_work_dir(settings, job.id)
        else:
            storage.remove_job_files(settings, job.id)
    except Exception:
        log.exception("purge failed for job %s", job.id)
        return
    job.files_purged_at = utcnow()
    job.disk_bytes = 0
    if not work_only:
        job.file_relpath = None


def cleanup_files(session: Session, settings: Settings) -> dict[str, int]:
    """Apply retention. Safe around active transfers: an in-flight stream keeps its open fd, and jobs
    with a live transfer are skipped until it goes idle."""
    now = utcnow()
    stats = {"expired": 0, "purged": 0, "paused_expired": 0}

    # 1. Completed files past retention (or removed by the user).
    due = session.scalars(select(Job).where(Job.status == S.COMPLETED.value, Job.expires_at < now).limit(200).with_for_update(skip_locked=True)).all()
    for job in due:
        if _has_active_transfer(session, settings, job.id):
            continue
        move(session, job, S.EXPIRED, "File expired and was removed from the server", finished_at=job.finished_at)
        _purge(session, settings, job)
        stats["expired"] += 1

    # 2. Paused jobs abandoned for too long.
    stale_paused = session.scalars(
        select(Job).where(Job.status == S.PAUSED.value, Job.updated_at < now - timedelta(hours=settings.paused_max_age_hours))
        .limit(200).with_for_update(skip_locked=True)
    ).all()
    for job in stale_paused:
        move(session, job, S.EXPIRED, "Paused download expired; partial data removed")
        _purge(session, settings, job)
        stats["paused_expired"] += 1

    # 3. Files still on disk for jobs that will never use them (canceled, expired, user-removed, aged-out failures).
    leftovers = session.scalars(
        select(Job).where(
            Job.files_purged_at.is_(None),
            or_(
                Job.status.in_([S.CANCELED.value, S.EXPIRED.value]),
                Job.deleted_at.is_not(None),
                (Job.status == S.FAILED.value) & (Job.finished_at < now - timedelta(hours=settings.failed_partial_retention_hours)),
            ),
        ).limit(200).with_for_update(skip_locked=True)
    ).all()
    for job in leftovers:
        if _has_active_transfer(session, settings, job.id):
            continue
        _purge(session, settings, job)
        stats["purged"] += 1
    return stats


def cleanup_records(session: Session, settings: Settings) -> None:
    now = utcnow()
    session.execute(delete(DownloadTicket).where(DownloadTicket.expires_at < now - timedelta(hours=1)))
    session.execute(delete(Inspection).where(Inspection.expires_at < now - timedelta(hours=1)))
    session.execute(delete(RefreshToken).where(RefreshToken.expires_at < now - timedelta(days=1)))


def remove_orphan_directories(session: Session, settings: Settings, min_age_seconds: int = 86400) -> int:
    """Delete ``jobs/<uuid>`` directories that have no database row (older than ``min_age_seconds``)."""
    root = storage.media_root(settings) / "jobs"
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        try:
            job_id = uuid.UUID(entry.name)
        except ValueError:
            continue
        if entry.is_symlink() or time.time() - entry.stat().st_mtime < min_age_seconds:
            continue
        if session.get(Job, job_id) is None:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed
