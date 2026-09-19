"""End-to-end worker behaviour against a fake yt-dlp: real subprocesses, real process groups, real DB."""

import os
import threading
import time
import uuid

import pytest

from app.db import session_scope
from app.models import Job
from app.security import utcnow
from app.services import storage
from app.worker.executor import execute_download
from tests.helpers import factory, get, make_due, pid_alive, wait_for


@pytest.fixture(autouse=True)
def fast(settings, monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_seconds", 0)
    monkeypatch.setattr(settings, "kill_grace_seconds", 2)
    monkeypatch.setattr(settings, "max_concurrent_jobs_global", 5)
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 5)


def start(settings, jid, mode):
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("outcome", execute_download(settings, uuid.UUID(str(jid)), "w", factory(mode))))
    t.start()
    return t, box


def test_successful_download_publishes_file(alice, new_job, settings):
    job = new_job(alice)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("ok")) == "completed"
    j = get(job["id"])
    assert j.status == "completed" and j.progress_percent == 100 and j.file_size == 4096 and j.disk_bytes == 4096
    assert j.file_relpath.endswith("/final/media.mp4") and j.file_name == "Test video.mp4" and j.expires_at > utcnow()
    assert not storage.work_dir(settings, j.id).exists()          # scratch data cleaned
    assert (storage.media_root(settings) / j.file_relpath).stat().st_mode & 0o077 == 0  # private
    api = alice.get(f"/api/jobs/{job['id']}").json()
    assert api["file"] == {"name": "Test video.mp4", "size": 4096, "mime": "video/mp4"} and api["actions"]["download"]
    assert api["delivery"]["state"] == "ready"


def test_cancel_kills_whole_process_group(alice, new_job, settings, tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    job = new_job(alice)
    t, box = start(settings, job["id"], "slow")
    child_pid = int(wait_for(lambda: pidfile.exists() and pidfile.read_text() or None))
    assert pid_alive(child_pid)
    assert alice.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "canceling"
    t.join(15)
    assert box["outcome"] == "canceled"
    assert not pid_alive(child_pid)                                   # grandchild (aria2c stand-in) is gone too
    j = get(job["id"])
    assert j.status == "canceled" and j.file_relpath is None and j.disk_bytes == 0
    assert not storage.job_dir(settings, j.id).exists()               # canceled jobs leave no partial data


def test_cancel_during_late_finish_never_completes(alice, new_job, settings, monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_seconds", 60)          # worker will not notice the cancel mid-run
    monkeypatch.setenv("FAKE_DELAY", "1.5")
    job = new_job(alice)
    t, box = start(settings, job["id"], "late")
    wait_for(lambda: get(job["id"]).status == "running")
    alice.post(f"/api/jobs/{job['id']}/cancel")
    t.join(15)
    assert box["outcome"] == "canceled"
    j = get(job["id"])
    assert j.status == "canceled" and j.file_relpath is None and not storage.job_dir(settings, j.id).exists()


def test_pause_keeps_partial_data_and_resume_reuses_it(alice, new_job, settings, monkeypatch):
    job = new_job(alice)
    t, box = start(settings, job["id"], "slow")
    work = storage.work_dir(settings, uuid.UUID(job["id"]))
    wait_for(lambda: (work / "vid.mp4.part").exists() and get(job["id"]).status == "running")
    assert alice.post(f"/api/jobs/{job['id']}/pause").json()["status"] == "pausing"
    t.join(15)
    assert box["outcome"] == "paused"
    j = get(job["id"])
    assert j.status == "paused" and j.attempts == 0                     # a pause does not consume the retry budget
    assert (work / "vid.mp4.part").read_bytes() == b"p" * 1000          # partial file untouched
    assert alice.post(f"/api/jobs/{job['id']}/resume").json()["status"] == "queued"
    make_due(job["id"])
    # the "resume" fake only succeeds if the previous partial data is still present
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("resume")) == "completed"


def test_transient_failures_retry_with_backoff_then_fail(alice, new_job, settings):
    job = new_job(alice)
    jid = uuid.UUID(job["id"])
    for attempt in (1, 2):
        assert execute_download(settings, jid, "w", factory("net")) == "retry"
        j = get(jid)
        assert j.status == "queued" and j.attempts == attempt and j.next_attempt_at > utcnow() and j.enqueued_at is None
        assert j.error_code == "network"
        assert alice.get(f"/api/jobs/{jid}").json()["retry_at"] is not None
        make_due(jid)
    assert execute_download(settings, jid, "w", factory("net")) == "failed"     # max_attempts=3: bounded
    j = get(jid)
    assert j.status == "failed" and j.attempts == 3 and j.error_message == "A network error interrupted the download."
    api = alice.get(f"/api/jobs/{jid}").json()
    assert api["actions"]["retry"] and "503" not in str(api)
    assert "HTTP Error 503" in (j.debug_tail or "")                       # operators keep the detail; the API never sends it
    assert alice.post(f"/api/jobs/{jid}/retry").json()["status"] == "queued"
    assert get(jid).attempts == 0


def test_permanent_failure_does_not_retry(alice, new_job, settings):
    job = new_job(alice)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("private")) == "failed"
    j = get(job["id"])
    assert j.attempts == 1 and j.error_code == "restricted" and "Sign in" not in j.error_message


def test_missing_output_is_reported(alice, new_job, settings):
    job = new_job(alice)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("empty")) == "failed"
    assert get(job["id"]).error_code == "no_output"


def test_unsupported_container_is_reported_and_cleaned(alice, new_job, settings):
    job = new_job(alice)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("weird")) == "failed"
    j = get(job["id"])
    assert j.error_code == "unsupported_file_type" and not storage.job_dir(settings, j.id).exists()


def test_max_runtime_kills_process_and_fails(alice, new_job, settings, tmp_path, monkeypatch):
    pidfile = tmp_path / "c.pid"
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    monkeypatch.setattr(settings, "job_max_runtime_seconds", 1)
    job = new_job(alice)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("slow")) == "failed"
    j = get(job["id"])
    assert j.error_code == "timeout" and j.attempts == 1                 # timeouts are not retried
    assert not pid_alive(int(pidfile.read_text()))
    assert not storage.job_dir(settings, j.id).exists()


def test_size_limit_stops_runaway_download(alice, new_job, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_file_bytes", 1024 * 1024)       # 1 MiB (limit trips at 2 MiB + 64 MiB slack ...)
    monkeypatch.setattr(storage, "dir_size", lambda p: 200 * 1024 * 1024)
    job = new_job(alice)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("big")) == "failed"
    assert get(job["id"]).error_code == "too_large"


def test_user_quota_enforced_while_downloading(alice, new_job, settings, monkeypatch):
    job = new_job(alice)
    monkeypatch.setattr(settings, "user_quota_bytes", 100)
    monkeypatch.setattr(storage, "dir_size", lambda p: 5000)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("big")) == "failed"
    assert get(job["id"]).error_code == "quota_exceeded"


def test_lease_loss_stops_worker_without_touching_state(alice, new_job, settings):
    job = new_job(alice)
    jid = uuid.UUID(job["id"])
    t, box = start(settings, jid, "slow")
    wait_for(lambda: get(jid).status == "running")
    with session_scope() as s:  # reconciler gave the job to someone else
        s.execute(Job.__table__.update().where(Job.id == jid).values(lease_token=uuid.uuid4()))
    t.join(15)
    assert box["outcome"] == "lost"
    assert get(jid).status == "running"


def test_worker_rechecks_url_policy(alice, new_job, settings, monkeypatch):
    job = new_job(alice)
    monkeypatch.setattr(settings, "allowed_source_hosts", ["vimeo.com"])   # allow-list tightened after queueing
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("ok")) == "failed"
    assert get(job["id"]).error_code == "url_rejected"


def test_production_requires_egress_proxy(alice, new_job, settings, monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    job = new_job(alice)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("ok")) == "failed"
    assert get(job["id"]).error_code == "egress_unconfigured"


def test_deferred_when_disk_is_low(alice, new_job, settings, monkeypatch):
    monkeypatch.setattr(settings, "min_free_disk_bytes", 10**18)
    job = new_job(alice)
    assert execute_download(settings, uuid.UUID(job["id"]), "w", factory("ok")) == "deferred"
    assert get(job["id"]).status == "queued"
