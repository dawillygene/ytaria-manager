import uuid
from datetime import timedelta

import pytest

from app.db import session_scope
from app.models import Job, Transfer
from app.security import utcnow
from app.services import reconcile, storage
from tests.helpers import complete_with_file, get


def age(job_id, **values):
    with session_scope() as s:
        s.query(Job).filter(Job.id == uuid.UUID(str(job_id))).update(values)


def test_completed_files_expire_and_are_deleted(alice, new_job, settings):
    job = new_job(alice)
    complete_with_file(settings, job["id"])
    j = get(job["id"])
    path = storage.media_root(settings) / j.file_relpath
    assert path.exists() and alice.get("/api/usage").json()["used_bytes"] == j.file_size
    with session_scope() as s:                                        # not yet due: untouched
        assert reconcile.cleanup_files(s, settings)["expired"] == 0
    age(job["id"], expires_at=utcnow() - timedelta(minutes=1))
    with session_scope() as s:
        assert reconcile.cleanup_files(s, settings)["expired"] == 1
    j = get(job["id"])
    assert j.status == "expired" and not path.exists() and not storage.job_dir(settings, j.id).exists() and j.disk_bytes == 0
    api = alice.get(f"/api/jobs/{job['id']}").json()
    assert api["status"] == "expired" and api["file"] is None and api["actions"]["retry"] and not api["actions"]["download"]
    assert alice.get(f"/api/jobs/{job['id']}/file").status_code == 409
    assert alice.get("/api/usage").json()["used_bytes"] == 0
    with session_scope() as s:                                        # idempotent
        assert reconcile.cleanup_files(s, settings) == {"expired": 0, "purged": 0, "paused_expired": 0}


def test_active_transfer_defers_cleanup(alice, new_job, settings):
    job = new_job(alice)
    complete_with_file(settings, job["id"])
    age(job["id"], expires_at=utcnow() - timedelta(minutes=1))
    with session_scope() as s:
        s.add(Transfer(job_id=uuid.UUID(job["id"]), user_id=uuid.UUID(alice.user_id), last_seen_at=utcnow()))
    with session_scope() as s:
        assert reconcile.cleanup_files(s, settings)["expired"] == 0
    assert get(job["id"]).status == "completed"
    with session_scope() as s:                                        # transfer went idle
        s.query(Transfer).update({"last_seen_at": utcnow() - timedelta(hours=1)})
    with session_scope() as s:
        assert reconcile.cleanup_files(s, settings)["expired"] == 1


def test_stream_in_flight_survives_unlink(alice, new_job, settings):
    """A transfer that already opened the file keeps reading after cleanup unlinks it."""
    import os

    job = new_job(alice)
    complete_with_file(settings, job["id"], data=b"z" * 5000)
    j = get(job["id"])
    fd, _ = storage.open_stored_file(settings, j.file_relpath)
    age(job["id"], expires_at=utcnow() - timedelta(minutes=1))
    with session_scope() as s:
        reconcile.cleanup_files(s, settings)
    try:
        assert os.read(fd, 5000) == b"z" * 5000
    finally:
        os.close(fd)


def test_user_removed_jobs_are_purged(alice, new_job, settings):
    job = new_job(alice)
    complete_with_file(settings, job["id"])
    assert alice.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert storage.job_dir(settings, uuid.UUID(job["id"])).exists()   # API never touches storage; the sweeper does
    with session_scope() as s:
        assert reconcile.cleanup_files(s, settings)["purged"] == 1
    assert not storage.job_dir(settings, uuid.UUID(job["id"])).exists()


def test_failed_partials_kept_for_retry_window_then_purged(alice, new_job, settings):
    job = new_job(alice)
    jid = uuid.UUID(job["id"])
    work = storage.ensure_job_dirs(settings, jid)
    (work / "vid.mp4.part").write_bytes(b"p" * 100)
    age(jid, status="failed", finished_at=utcnow() - timedelta(hours=1), disk_bytes=100)
    with session_scope() as s:
        assert reconcile.cleanup_files(s, settings)["purged"] == 0
    assert (work / "vid.mp4.part").exists()
    age(jid, finished_at=utcnow() - timedelta(hours=settings.failed_partial_retention_hours + 1))
    with session_scope() as s:
        assert reconcile.cleanup_files(s, settings)["purged"] == 1
    assert not work.exists() and get(jid).status == "failed" and get(jid).disk_bytes == 0


def test_abandoned_paused_jobs_expire(alice, new_job, settings):
    job = new_job(alice)
    jid = uuid.UUID(job["id"])
    alice.post(f"/api/jobs/{jid}/pause")
    work = storage.ensure_job_dirs(settings, jid)
    (work / "x.part").write_bytes(b"1")
    age(jid, updated_at=utcnow() - timedelta(hours=settings.paused_max_age_hours + 1))
    with session_scope() as s:
        assert reconcile.cleanup_files(s, settings)["paused_expired"] == 1
    assert get(jid).status == "expired" and not work.exists()


def test_canceled_while_queued_purges_leftovers(alice, new_job, settings):
    job = new_job(alice)
    jid = uuid.UUID(job["id"])
    work = storage.ensure_job_dirs(settings, jid)
    (work / "x.part").write_bytes(b"1")
    alice.post(f"/api/jobs/{jid}/cancel")
    with session_scope() as s:
        assert reconcile.cleanup_files(s, settings)["purged"] == 1
    assert not storage.job_dir(settings, jid).exists()


def test_orphan_directories_removed_only_when_old_and_unknown(alice, new_job, settings):
    import os, time

    known = uuid.UUID(new_job(alice)["id"])
    for name in (known, uuid.uuid4(), uuid.uuid4()):
        d = storage.job_dir(settings, name)
        d.mkdir(parents=True)
    old = storage.job_dir(settings, uuid.uuid4())
    old.mkdir(parents=True)
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    os.utime(storage.job_dir(settings, known), (time.time() - 10 * 86400,) * 2)
    with session_scope() as s:
        assert reconcile.remove_orphan_directories(s, settings) == 1
    assert not old.exists() and storage.job_dir(settings, known).exists()


def test_advisory_lock_prevents_concurrent_sweeps(settings):
    with session_scope() as first:
        assert reconcile.try_advisory_lock(first, reconcile.CLEANUP_LOCK)
        with session_scope() as second:
            assert not reconcile.try_advisory_lock(second, reconcile.CLEANUP_LOCK)
