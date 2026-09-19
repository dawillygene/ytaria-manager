from __future__ import annotations

from celery import Celery

from ..config import get_settings
from ..logging_utils import configure_logging

settings = get_settings()
configure_logging()

celery = Celery("ytaria", broker=settings.redis_url, include=["app.worker.tasks"])
celery.conf.update(
    task_default_queue="downloads",
    task_routes={"ytaria.run_inspection": {"queue": "inspect"}, "ytaria.reconcile": {"queue": "maintenance"},
                 "ytaria.cleanup": {"queue": "maintenance"}},
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=False,
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    # Time limits are a backstop; the runner enforces the real (graceful) limit itself.
    task_time_limit=settings.job_max_runtime_seconds + 600,
    task_soft_time_limit=settings.job_max_runtime_seconds + 300,
    broker_transport_options={"visibility_timeout": settings.job_max_runtime_seconds + 3600},
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
    timezone="UTC",
    beat_schedule={
        "reconcile": {"task": "ytaria.reconcile", "schedule": float(settings.reconcile_interval_seconds)},
        "cleanup": {"task": "ytaria.cleanup", "schedule": float(settings.cleanup_interval_seconds)},
    },
)
