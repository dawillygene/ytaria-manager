from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Inspection, Job, Transfer
from ..schemas import ActionsOut, DeliveryOut, ErrorOut, FileOut, InspectionOut, JobOut, OptionOut
from ..security import utcnow
from ..states import DELETABLE, RETRYABLE, JobStatus

S = JobStatus


def _delivery(job: Job, transfers: list[Transfer], settings: Settings) -> DeliveryOut:
    if job.status != S.COMPLETED.value or not job.file_relpath:
        return DeliveryOut(state="unavailable")
    saved = [t.device_saved_at for t in transfers if t.device_saved_at]
    if saved:
        return DeliveryOut(state="saved", saved_at=max(saved))
    cutoff = utcnow() - timedelta(seconds=settings.transfer_active_window_seconds)
    latest = max(transfers, key=lambda t: t.started_at, default=None)
    if latest is not None and latest.server_completed_at is None and latest.device_failed_at is None and latest.last_seen_at > cutoff:
        return DeliveryOut(state="transferring")  # the newest attempt is still streaming
    if any(t.server_completed_at for t in transfers):
        return DeliveryOut(state="sent")
    return DeliveryOut(state="ready")


def serialize_jobs(session: Session, settings: Settings, jobs: list[Job]) -> list[JobOut]:
    by_job: dict[uuid.UUID, list[Transfer]] = {}
    completed_ids = [j.id for j in jobs if j.status == S.COMPLETED.value]
    if completed_ids:
        for t in session.scalars(select(Transfer).where(Transfer.job_id.in_(completed_ids))):
            by_job.setdefault(t.job_id, []).append(t)
    now = utcnow()
    out = []
    for j in jobs:
        status = JobStatus(j.status)
        retry_at = j.next_attempt_at if status == S.QUEUED and j.attempts > 0 and j.next_attempt_at > now else None
        error = ErrorOut(code=j.error_code, message=j.error_message or "") if j.error_code else None
        downloadable = status == S.COMPLETED and bool(j.file_relpath)
        out.append(JobOut(
            id=j.id, url=j.url, host=j.host, title=j.title, duration_seconds=j.duration_seconds, selection=j.selection,
            status=j.status, stage=j.stage, progress_percent=round(j.progress_percent, 1), downloaded_bytes=j.downloaded_bytes,
            total_bytes=j.total_bytes, speed_bps=j.speed_bps, eta_seconds=j.eta_seconds, attempt=j.attempts,
            max_attempts=j.max_attempts, retry_at=retry_at, error=error, created_at=j.created_at, started_at=j.started_at,
            finished_at=j.finished_at, expires_at=j.expires_at,
            file=FileOut(name=j.file_name or "download", size=j.file_size or 0, mime=j.file_mime or "application/octet-stream") if downloadable else None,
            delivery=_delivery(j, by_job.get(j.id, []), settings),
            actions=ActionsOut(
                pause=status in (S.QUEUED, S.RUNNING), resume=status == S.PAUSED,
                cancel=status in (S.QUEUED, S.RUNNING, S.PAUSED, S.PAUSING), retry=status in RETRYABLE,
                delete=status in DELETABLE, download=downloadable),
        ))
    return out


def serialize_inspection(insp: Inspection) -> InspectionOut:
    result = insp.result or {}
    return InspectionOut(
        id=insp.id, status=insp.status, url=insp.url, title=result.get("title"), duration_seconds=result.get("duration_seconds"),
        extractor=result.get("extractor"), options=[OptionOut(**o) for o in result.get("options", [])],
        error=ErrorOut(code=insp.error_code, message=insp.error_message or "") if insp.error_code else None,
        expires_at=insp.expires_at,
    )
