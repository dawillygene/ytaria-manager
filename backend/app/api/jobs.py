from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Response
from sqlalchemy import select

from .. import ratelimit
from ..models import Inspection
from ..schemas import EventOut, InspectionCreate, InspectionOut, JobCreate, JobOut, JobPage, UsageOut
from ..services import inspections as inspsvc
from ..services import jobs as jobsvc
from ..services.errors import NotFound
from ..services.serialize import serialize_inspection, serialize_jobs
from ..services.urlpolicy import UrlRejected, validate_source_url
from ..states import ACTIVE_VALUES
from ..worker.dispatch import get_dispatcher
from .deps import SessionDep, SettingsDep, UserDep, api_error

router = APIRouter(tags=["jobs"])


def _dispatch_job(session, job_id: uuid.UUID) -> None:
    """Best effort. If the broker is down the job stays queued with enqueued_at NULL and the
    reconciler publishes it later."""
    from sqlalchemy import update

    from ..models import Job
    from ..security import utcnow

    try:
        get_dispatcher().download(job_id)
    except Exception:
        return
    session.execute(update(Job).where(Job.id == job_id, Job.status == "queued", Job.enqueued_at.is_(None)).values(enqueued_at=utcnow()))
    session.commit()


# --- inspections --------------------------------------------------------------------------------


@router.post("/inspections", response_model=InspectionOut, status_code=202)
def create_inspection(body: InspectionCreate, response: Response, session: SessionDep, settings: SettingsDep, user: UserDep) -> InspectionOut:
    ratelimit.check("inspect", str(user.id), settings.rl_inspect_user)
    try:
        url = validate_source_url(body.url, allowed_hosts=settings.allowed_source_hosts, allowed_ports=settings.allowed_ports)
    except UrlRejected as exc:
        raise api_error(422, exc.code, exc.message) from None
    insp, is_new = inspsvc.create_inspection(session, settings, user, url)
    session.commit()
    if is_new:
        try:
            get_dispatcher().inspection(insp.id)
            insp.enqueued_at = insp.created_at
            session.commit()
        except Exception:
            pass  # reconciler will publish it
    if not is_new:
        response.status_code = 200
    return serialize_inspection(insp)


@router.get("/inspections/{inspection_id}", response_model=InspectionOut)
def get_inspection(inspection_id: uuid.UUID, session: SessionDep, user: UserDep) -> InspectionOut:
    insp = session.execute(select(Inspection).where(Inspection.id == inspection_id, Inspection.user_id == user.id)).scalar_one_or_none()
    if insp is None:
        raise NotFound("Inspection not found.")
    return serialize_inspection(insp)


# --- jobs ---------------------------------------------------------------------------------------


@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(
    body: JobCreate,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    user: UserDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_\-:.]+$")] = None,
) -> JobOut:
    ratelimit.check("job-create", str(user.id), settings.rl_job_create_user)
    job, created = jobsvc.create_job(session, settings, user, body.inspection_id, body.selection, idempotency_key)
    session.commit()
    if created:
        _dispatch_job(session, job.id)
    else:
        response.status_code = 200  # replay / duplicate: same job, nothing new queued
    return serialize_jobs(session, settings, [job])[0]


@router.get("/jobs", response_model=JobPage)
def list_jobs(
    session: SessionDep, settings: SettingsDep, user: UserDep,
    scope: Literal["all", "active", "history", "ready"] = "all",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> JobPage:
    items, total = jobsvc.list_jobs(session, user.id, scope=scope, page=page, page_size=page_size)
    return JobPage(items=serialize_jobs(session, settings, items), total=total, page=page, page_size=page_size)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, session: SessionDep, settings: SettingsDep, user: UserDep) -> JobOut:
    return serialize_jobs(session, settings, [jobsvc.get_job(session, user.id, job_id)])[0]


@router.get("/jobs/{job_id}/events", response_model=list[EventOut])
def job_events(job_id: uuid.UUID, session: SessionDep, user: UserDep) -> list[EventOut]:
    return [EventOut.model_validate(e) for e in jobsvc.list_events(session, user.id, job_id)]




@router.post("/jobs/{job_id}/pause", response_model=JobOut)
def pause(job_id: uuid.UUID, session: SessionDep, settings: SettingsDep, user: UserDep) -> JobOut:
    job = jobsvc.pause_job(session, user.id, job_id)
    session.commit()
    return serialize_jobs(session, settings, [job])[0]


@router.post("/jobs/{job_id}/resume", response_model=JobOut)
def resume(job_id: uuid.UUID, session: SessionDep, settings: SettingsDep, user: UserDep) -> JobOut:
    job = jobsvc.resume_job(session, user.id, job_id)
    session.commit()
    if job.status == "queued":
        _dispatch_job(session, job.id)
    return serialize_jobs(session, settings, [job])[0]


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel(job_id: uuid.UUID, session: SessionDep, settings: SettingsDep, user: UserDep) -> JobOut:
    job = jobsvc.cancel_job(session, user.id, job_id)
    session.commit()
    return serialize_jobs(session, settings, [job])[0]


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry(job_id: uuid.UUID, session: SessionDep, settings: SettingsDep, user: UserDep) -> JobOut:
    job = jobsvc.retry_job(session, settings, user, job_id)
    session.commit()
    if job.status == "queued":
        _dispatch_job(session, job.id)
    return serialize_jobs(session, settings, [job])[0]


@router.delete("/jobs/{job_id}", status_code=204)
def delete(job_id: uuid.UUID, session: SessionDep, user: UserDep) -> Response:
    jobsvc.delete_job(session, user.id, job_id)
    session.commit()
    return Response(status_code=204)


@router.get("/usage", response_model=UsageOut)
def usage(session: SessionDep, settings: SettingsDep, user: UserDep) -> UsageOut:
    return UsageOut(
        used_bytes=jobsvc.user_disk_usage(session, user.id), quota_bytes=jobsvc.user_quota(settings, user),
        active_jobs=jobsvc.active_count(session, user.id), max_active_jobs=settings.max_active_jobs_per_user,
        completed_retention_hours=settings.completed_retention_hours, max_file_bytes=settings.max_file_bytes,
        supported_sites=settings.allowed_source_hosts,
    )
