"""Job state machine.

PostgreSQL is the source of truth. Every status change goes through
``services.jobs.transition`` which validates against ``TRANSITIONS`` and performs a
conditional UPDATE, so concurrent writers (API, workers, reconciler) cannot resurrect a
job that has already moved on.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"          # committed, waiting for (or between) worker attempts
    RUNNING = "running"        # a worker holds the lease and a subprocess is (being) started
    PAUSING = "pausing"        # pause requested; worker has not stopped the process yet
    PAUSED = "paused"          # process stopped, partial data retained
    CANCELING = "canceling"    # cancel requested; worker has not stopped the process yet
    CANCELED = "canceled"
    COMPLETED = "completed"    # file is on the server and ready to transfer
    FAILED = "failed"
    EXPIRED = "expired"        # file (or paused partial data) removed by retention


S = JobStatus

TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    # queued -> paused/canceled are direct: no process exists yet. queued -> failed covers
    # non-retryable pre-flight failures (quota, policy) found by the worker before it runs anything.
    S.QUEUED: frozenset({S.RUNNING, S.PAUSED, S.CANCELED, S.FAILED}),
    S.RUNNING: frozenset({S.COMPLETED, S.FAILED, S.QUEUED, S.PAUSING, S.CANCELING}),
    # pausing -> completed: the process happened to finish before it could be stopped; keep the work.
    # canceling -> completed is deliberately NOT allowed (cancel always wins).
    S.PAUSING: frozenset({S.PAUSED, S.CANCELING, S.COMPLETED, S.FAILED, S.QUEUED}),
    S.PAUSED: frozenset({S.QUEUED, S.CANCELED, S.EXPIRED}),
    S.CANCELING: frozenset({S.CANCELED}),
    S.CANCELED: frozenset({S.QUEUED}),   # manual retry, starts from scratch
    S.FAILED: frozenset({S.QUEUED}),     # manual retry
    S.COMPLETED: frozenset({S.EXPIRED}),
    S.EXPIRED: frozenset({S.QUEUED}),    # re-download
}

ACTIVE = frozenset({S.QUEUED, S.RUNNING, S.PAUSING, S.PAUSED, S.CANCELING})
LEASED = frozenset({S.RUNNING, S.PAUSING, S.CANCELING})
TERMINAL = frozenset({S.COMPLETED, S.FAILED, S.CANCELED, S.EXPIRED})
RETRYABLE = frozenset({S.FAILED, S.CANCELED, S.EXPIRED})
DELETABLE = TERMINAL


def can_transition(src: JobStatus, dst: JobStatus) -> bool:
    return dst in TRANSITIONS[src]


class InspectionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ACTIVE_VALUES = sorted(s.value for s in ACTIVE)
