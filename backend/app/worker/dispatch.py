"""Publishing work to Celery. Replaceable in tests. Failures are non-fatal: the reconciler re-publishes."""

from __future__ import annotations

import uuid
from typing import Protocol


class Dispatcher(Protocol):
    def download(self, job_id: uuid.UUID) -> None: ...
    def inspection(self, inspection_id: uuid.UUID) -> None: ...


class CeleryDispatcher:
    def download(self, job_id: uuid.UUID) -> None:
        from .celery_app import celery

        celery.send_task("ytaria.run_download", args=[str(job_id)], queue="downloads")

    def inspection(self, inspection_id: uuid.UUID) -> None:
        from .celery_app import celery

        celery.send_task("ytaria.run_inspection", args=[str(inspection_id)], queue="inspect")


_dispatcher: Dispatcher = CeleryDispatcher()


def get_dispatcher() -> Dispatcher:
    return _dispatcher


def set_dispatcher(dispatcher: Dispatcher) -> None:
    global _dispatcher
    _dispatcher = dispatcher
