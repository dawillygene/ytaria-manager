import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.models import Job, JobEvent
from app.security import utcnow
from app.services import jobs as jobsvc
from app.states import TRANSITIONS, ACTIVE, JobStatus, can_transition
from tests.conftest import Session, make_inspection
from tests.helpers import complete_with_file, get, make_due

SENSITIVE = {"file_relpath", "debug_tail", "lease_token", "lease_owner", "command", "output_dir", "path", "destination", "cookies", "idempotency_key", "user_id"}


def test_create_job_shape_and_no_sensitive_fields(alice, new_job, dispatcher):
    job = new_job(alice)
    assert job["status"] == "queued" and job["selection"] == "best" and job["actions"]["cancel"] and job["actions"]["pause"]
    assert not (SENSITIVE & set(job)), set(job) & SENSITIVE
    assert not any(k in str(job).lower() for k in ("/tmp", "media_root", "yt-dlp", "aria2c"))
    assert dispatcher.downloads == [uuid.UUID(job["id"])]
    detail = alice.get(f"/api/jobs/{job['id']}").json()
    assert detail["id"] == job["id"]
    events = alice.get(f"/api/jobs/{job['id']}/events").json()
    assert events[0]["to_status"] == "queued"


def test_idempotency_key_replay_and_natural_dedupe(alice, new_job, dispatcher):
    a = new_job(alice, url="https://www.youtube.com/watch?v=same", idem="key-12345678")
    b = new_job(alice, url="https://www.youtube.com/watch?v=same", idem="key-12345678")
    c = new_job(alice, url="https://www.youtube.com/watch?v=same", idem="other-key-9999")  # different key, same active job
    assert a["id"] == b["id"] == c["id"]
    assert len(dispatcher.downloads) == 1
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1


def test_concurrent_identical_submissions_create_one_job(alice, dispatcher):
    insp = make_inspection(alice.user_id, url="https://www.youtube.com/watch?v=race")
    results, barrier = [], threading.Barrier(6)

    def go(i):
        sess = Session.__new__(Session)
        sess.__dict__.update(alice.__dict__)
        barrier.wait()
        r = alice.http.post("/api/jobs", json={"inspection_id": str(insp), "selection": "best"}, headers=alice.h(**{"Idempotency-Key": f"race-key-{i:04d}"}))
        results.append((r.status_code, r.json().get("id")))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert {code for code, _ in results} <= {200, 201}
    assert len({jid for _, jid in results}) == 1
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1


def test_selection_is_validated_against_inspection(alice, bob):
    insp = make_inspection(alice.user_id)
    for bad in ("h1080", "h4320", "audio_m4a", "--exec=x", "best; rm -rf /", "x"):
        r = alice.post("/api/jobs", {"inspection_id": str(insp), "selection": bad})
        assert r.status_code == 422, (bad, r.text)
    assert alice.post("/api/jobs", {"inspection_id": str(insp), "selection": "h720", "extra": "x"}).status_code == 422
    # someone else's inspection is indistinguishable from a missing one
    assert bob.post("/api/jobs", {"inspection_id": str(insp), "selection": "best"}).status_code == 404
    pending = make_inspection(alice.user_id, url="https://www.youtube.com/watch?v=p", status="pending")
    assert alice.post("/api/jobs", {"inspection_id": str(pending), "selection": "best"}).json()["error"]["code"] == "inspection_not_ready"


def test_queue_limit_and_quota(alice, new_job, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs_per_user", 2)
    new_job(alice)
    new_job(alice)
    insp = make_inspection(alice.user_id, url="https://www.youtube.com/watch?v=third")
    r = alice.post("/api/jobs", {"inspection_id": str(insp), "selection": "best"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "queue_limit"
    monkeypatch.setattr(settings, "max_active_jobs_per_user", 10)
    monkeypatch.setattr(settings, "user_quota_bytes", 500)  # estimated size of "best" is 1000
    r = alice.post("/api/jobs", {"inspection_id": str(insp), "selection": "best"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "quota_exceeded"


def test_broker_outage_leaves_job_queued_and_reconciler_publishes(alice, dispatcher, settings):
    from app.services import reconcile

    dispatcher.fail = True
    job = alice.post("/api/jobs", {"inspection_id": str(make_inspection(alice.user_id)), "selection": "best"}).json()
    assert job["status"] == "queued" and get(job["id"]).enqueued_at is None
    dispatcher.fail = False
    with session_scope() as s:
        assert reconcile.redispatch_queued(s, settings, dispatcher.download, dispatcher.inspection) == 1
    assert dispatcher.downloads == [uuid.UUID(job["id"])] and get(job["id"]).enqueued_at is not None
    with session_scope() as s:  # not re-published while its message is fresh
        assert reconcile.redispatch_queued(s, settings, dispatcher.download, dispatcher.inspection) == 0


def test_state_machine_rejects_every_undeclared_transition(db):
    for src in JobStatus:
        for dst in JobStatus:
            if src != dst and dst not in TRANSITIONS[src]:
                assert not can_transition(src, dst)
    assert not can_transition(JobStatus.CANCELING, JobStatus.COMPLETED)  # cancel always wins
    assert not can_transition(JobStatus.COMPLETED, JobStatus.RUNNING)
    assert can_transition(JobStatus.PAUSING, JobStatus.COMPLETED)
    job = Job(user_id=uuid.uuid4(), url="u", url_hash="h", host="h", selection="best", status="completed")
    with pytest.raises(jobsvc.InvalidTransition):
        jobsvc.move(db, job, JobStatus.RUNNING)


def test_controls_are_idempotent_and_consistent(alice, new_job, dispatcher):
    job = new_job(alice)
    jid = job["id"]
    assert alice.post(f"/api/jobs/{jid}/pause").json()["status"] == "paused"
    assert alice.post(f"/api/jobs/{jid}/pause").json()["status"] == "paused"
    r = alice.post(f"/api/jobs/{jid}/resume")
    assert r.json()["status"] == "queued" and len(dispatcher.downloads) == 2
    assert alice.post(f"/api/jobs/{jid}/resume").json()["status"] == "queued"  # already resumed
    assert alice.post(f"/api/jobs/{jid}/cancel").json()["status"] == "canceled"
    assert alice.post(f"/api/jobs/{jid}/cancel").json()["status"] == "canceled"
    assert alice.post(f"/api/jobs/{jid}/pause").status_code == 409
    assert alice.post(f"/api/jobs/{jid}/retry").json()["status"] == "queued"
    assert alice.post(f"/api/jobs/{jid}/retry").json()["status"] == "queued"  # idempotent
    assert alice.delete(f"/api/jobs/{jid}").status_code == 409  # must cancel first


def test_running_job_pause_and_cancel_are_two_phase(alice, new_job, settings):
    job = new_job(alice)
    with session_scope() as s:
        claim = jobsvc.claim_job(s, settings, uuid.UUID(job["id"]), "w1")
        assert claim.outcome == "claimed"
    assert alice.post(f"/api/jobs/{job['id']}/pause").json()["status"] == "pausing"
    assert alice.post(f"/api/jobs/{job['id']}/pause").json()["status"] == "pausing"
    assert alice.post(f"/api/jobs/{job['id']}/resume").status_code == 409
    assert alice.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "canceling"
    assert alice.post(f"/api/jobs/{job['id']}/pause").status_code == 409


def test_duplicate_task_delivery_claims_once(alice, new_job, settings):
    job = new_job(alice)
    jid = uuid.UUID(job["id"])
    outcomes = []
    for _ in range(2):
        with session_scope() as s:
            outcomes.append(jobsvc.claim_job(s, settings, jid, "w").outcome)
    assert outcomes == ["claimed", "skipped"]
    assert get(jid).attempts == 1


def test_parallel_claims_only_one_wins(alice, new_job, settings):
    jid = uuid.UUID(new_job(alice)["id"])
    outcomes, barrier = [], threading.Barrier(5)

    def go():
        barrier.wait()
        with session_scope() as s:
            outcomes.append(jobsvc.claim_job(s, settings, jid, "w").outcome)

    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(outcomes) == ["claimed"] + ["skipped"] * 4


def test_global_and_per_user_concurrency_limits(alice, bob, new_job, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_jobs_global", 2)
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 1)
    a1, a2, b1, b2 = (uuid.UUID(new_job(u)["id"]) for u in (alice, alice, bob, bob))

    def claim(j):
        with session_scope() as s:
            return jobsvc.claim_job(s, settings, j, "w").outcome

    assert claim(a1) == "claimed"
    assert claim(a2) == "deferred"          # per-user limit
    assert get(a2).enqueued_at is None and get(a2).next_attempt_at > utcnow()
    assert claim(b1) == "claimed"
    assert claim(b2) == "deferred"          # global limit (and per-user)
    assert get(a2).status == "queued"


def test_cancel_wins_over_racing_completion(alice, new_job, settings):
    jid = uuid.UUID(new_job(alice)["id"])
    with session_scope() as s:
        token = jobsvc.claim_job(s, settings, jid, "w").token
    assert alice.post(f"/api/jobs/{jid}/cancel").json()["status"] == "canceling"
    with session_scope() as s:  # the process finished just after the cancel request
        ok = jobsvc.complete_job(s, settings, jid, token, relpath="jobs/x/final/media.mp4", file_name="a.mp4", size=1, mime="video/mp4")
    assert ok is False
    with session_scope() as s:
        assert jobsvc.finish_stop(s, jid, token, "cancel") == "canceled"
    job = get(jid)
    assert job.status == "canceled" and job.file_relpath is None
    with session_scope() as s:  # and nothing can resurrect it
        assert jobsvc.complete_job(s, settings, jid, token, relpath="x", file_name="a", size=1, mime="m") is False
    assert get(jid).status == "canceled"


def test_pause_racing_completion_keeps_the_finished_work(alice, new_job, settings):
    jid = uuid.UUID(new_job(alice)["id"])
    with session_scope() as s:
        token = jobsvc.claim_job(s, settings, jid, "w").token
    alice.post(f"/api/jobs/{jid}/pause")
    with session_scope() as s:
        assert jobsvc.complete_job(s, settings, jid, token, relpath="jobs/x/final/media.mp4", file_name="a.mp4", size=1, mime="video/mp4")
    assert get(jid).status == "completed"


def test_stale_worker_cannot_write_after_recovery(alice, new_job, settings):
    from app.services import reconcile

    jid = uuid.UUID(new_job(alice)["id"])
    with session_scope() as s:
        old = jobsvc.claim_job(s, settings, jid, "w-old").token
    with session_scope() as s:
        s.execute(Job.__table__.update().where(Job.id == jid).values(lease_expires_at=utcnow() - timedelta(seconds=5)))
    with session_scope() as s:
        assert reconcile.recover_expired_leases(s, settings) == 1
    job = get(jid)
    assert job.status == "queued" and job.error_code == "worker_lost" and job.next_attempt_at > utcnow() and job.lease_token is None
    make_due(jid)
    with session_scope() as s:
        new = jobsvc.claim_job(s, settings, jid, "w-new")
    assert new.outcome == "claimed" and new.job.attempts == 2
    with session_scope() as s:  # the crashed worker wakes up: fenced out
        assert jobsvc.heartbeat(s, settings, jid, old) is None
        assert jobsvc.record_progress(s, jid, old, progress_percent=99.0) is False
        assert jobsvc.complete_job(s, settings, jid, old, relpath="x", file_name="a", size=1, mime="m") is False
    assert get(jid).status == "running" and get(jid).progress_percent < 99


def test_recovery_outcomes_by_state(alice, new_job, settings, monkeypatch):
    from app.services import reconcile

    monkeypatch.setattr(settings, "max_concurrent_jobs_global", 10)
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 10)

    ids = {}
    for name in ("run", "pausing", "canceling", "exhausted"):
        jid = uuid.UUID(new_job(alice)["id"])
        ids[name] = jid
        with session_scope() as s:
            jobsvc.claim_job(s, settings, jid, "w")
    alice.post(f"/api/jobs/{ids['pausing']}/pause")
    alice.post(f"/api/jobs/{ids['canceling']}/cancel")
    with session_scope() as s:
        s.execute(Job.__table__.update().where(Job.id == ids["exhausted"]).values(attempts=settings.max_attempts))
        s.execute(Job.__table__.update().values(lease_expires_at=utcnow() - timedelta(seconds=1)))
    with session_scope() as s:
        assert reconcile.recover_expired_leases(s, settings) == 4
    assert get(ids["run"]).status == "queued"
    assert get(ids["pausing"]).status == "paused"
    assert get(ids["canceling"]).status == "canceled"
    assert get(ids["exhausted"]).status == "failed" and get(ids["exhausted"]).error_code == "worker_lost"


def test_backoff_is_exponential_bounded_and_finite(settings):
    values = [jobsvc.backoff_seconds(settings, n) for n in range(1, 12)]
    assert max(values) <= settings.retry_backoff_cap_seconds * 1.25
    base = settings.retry_backoff_base_seconds
    assert base * 0.75 <= values[0] <= base * 1.25 and values[1] >= base * 2 * 0.75


def test_user_can_delete_terminal_jobs_only(alice, new_job, settings):
    job = new_job(alice)
    alice.post(f"/api/jobs/{job['id']}/cancel")
    assert alice.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert alice.get(f"/api/jobs/{job['id']}").status_code == 404
    assert alice.get("/api/jobs").json()["total"] == 0


def test_pagination_and_scopes(alice, new_job, settings):
    ids = [new_job(alice)["id"] for _ in range(4)]
    for jid in ids[:3]:
        alice.post(f"/api/jobs/{jid}/cancel")
    page1 = alice.get("/api/jobs?scope=history&page=1&page_size=2").json()
    page2 = alice.get("/api/jobs?scope=history&page=2&page_size=2").json()
    assert page1["total"] == 3 and len(page1["items"]) == 2 and len(page2["items"]) == 1
    assert {i["id"] for i in page1["items"]}.isdisjoint({i["id"] for i in page2["items"]})
    assert alice.get("/api/jobs?scope=active").json()["total"] == 1
    assert alice.get("/api/jobs?page_size=500").status_code == 422
