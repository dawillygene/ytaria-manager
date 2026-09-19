from __future__ import annotations

import logging
import uuid

from ..config import get_settings
from ..db import session_scope
from ..services import inspections, reconcile
from .celery_app import celery
from .dispatch import get_dispatcher
from .executor import execute_download

log = logging.getLogger("ytaria.tasks")


@celery.task(name="ytaria.run_download", acks_late=True)
def run_download(job_id: str) -> str:
    outcome = execute_download(get_settings(), uuid.UUID(job_id))
    log.info("download attempt finished outcome=%s", outcome, extra={"job_id": job_id})
    return outcome


@celery.task(name="ytaria.run_inspection", acks_late=True)
def run_inspection(inspection_id: str) -> None:
    with session_scope() as s:
        inspections.execute_inspection(s, get_settings(), uuid.UUID(inspection_id))


@celery.task(name="ytaria.reconcile")
def reconcile_task() -> dict:
    settings = get_settings()
    dispatcher = get_dispatcher()
    with session_scope() as s:
        if not reconcile.try_advisory_lock(s, reconcile.RECONCILE_LOCK):
            return {"skipped": True}
        recovered = reconcile.recover_expired_leases(s, settings)
        dispatched = reconcile.redispatch_queued(s, settings, dispatcher.download, dispatcher.inspection)
    return {"recovered": recovered, "dispatched": dispatched}


@celery.task(name="ytaria.cleanup")
def cleanup_task() -> dict:
    settings = get_settings()
    with session_scope() as s:
        if not reconcile.try_advisory_lock(s, reconcile.CLEANUP_LOCK):
            return {"skipped": True}
        stats = reconcile.cleanup_files(s, settings)
        reconcile.cleanup_records(s, settings)
        stats["orphans"] = reconcile.remove_orphan_directories(s, settings)
    return stats
