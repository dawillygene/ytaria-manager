"""Users must never be able to see or control each other's data, even with valid ids."""

import uuid

import pytest

from tests.conftest import Session, make_inspection
from tests.helpers import complete_with_file


@pytest.fixture
def victim_job(alice, new_job, settings):
    job = new_job(alice)
    complete_with_file(settings, job["id"])
    return job


def test_other_users_job_is_invisible_everywhere(alice, bob, victim_job):
    jid = victim_job["id"]
    for method, path in [
        ("get", f"/api/jobs/{jid}"), ("get", f"/api/jobs/{jid}/events"), ("get", f"/api/jobs/{jid}/file"),
        ("post", f"/api/jobs/{jid}/pause"), ("post", f"/api/jobs/{jid}/resume"), ("post", f"/api/jobs/{jid}/cancel"),
        ("post", f"/api/jobs/{jid}/retry"), ("post", f"/api/jobs/{jid}/download-ticket"), ("post", f"/api/jobs/{jid}/transfer"),
        ("delete", f"/api/jobs/{jid}"),
    ]:
        kwargs = {"json": {"state": "saved"}} if path.endswith("/transfer") else {}
        r = getattr(bob, method)(path, **kwargs) if method != "post" else bob.post(path, kwargs.get("json"))
        assert r.status_code == 404, (method, path, r.status_code)
    assert bob.get("/api/jobs").json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert bob.get("/api/usage").json()["used_bytes"] == 0
    # ... and the owner is unaffected
    assert alice.get(f"/api/jobs/{jid}").json()["status"] == "completed"


def test_not_found_is_identical_for_foreign_and_nonexistent_ids(alice, bob, victim_job):
    foreign = bob.get(f"/api/jobs/{victim_job['id']}")
    missing = bob.get(f"/api/jobs/{uuid.uuid4()}")
    assert foreign.status_code == missing.status_code == 404 and foreign.json() == missing.json()


def test_inspections_are_private(alice, bob):
    insp = alice.post("/api/inspections", {"url": "https://www.youtube.com/watch?v=abc"}).json()
    assert alice.get(f"/api/inspections/{insp['id']}").status_code == 200
    assert bob.get(f"/api/inspections/{insp['id']}").status_code == 404
    # the same URL from another user creates a separate inspection
    other = bob.post("/api/inspections", {"url": "https://www.youtube.com/watch?v=abc"}).json()
    assert other["id"] != insp["id"]


def test_same_url_by_two_users_makes_independent_jobs(alice, bob, new_job):
    a = new_job(alice, url="https://www.youtube.com/watch?v=shared")
    b = new_job(bob, url="https://www.youtube.com/watch?v=shared")
    assert a["id"] != b["id"]
    assert bob.post(f"/api/jobs/{b['id']}/cancel").json()["status"] == "canceled"
    assert alice.get(f"/api/jobs/{a['id']}").json()["status"] == "queued"


def test_idempotency_keys_are_scoped_per_user(alice, bob, new_job):
    a = new_job(alice, url="https://www.youtube.com/watch?v=k1", idem="shared-key-1234")
    b = new_job(bob, url="https://www.youtube.com/watch?v=k2", idem="shared-key-1234")
    assert a["id"] != b["id"]


def test_unauthenticated_access_is_rejected(client, victim_job):
    jid = victim_job["id"]
    for method, path in [("get", "/api/jobs"), ("get", f"/api/jobs/{jid}"), ("get", f"/api/jobs/{jid}/file"), ("get", "/api/usage"),
                         ("post", "/api/inspections"), ("post", "/api/jobs"), ("post", f"/api/jobs/{jid}/download-ticket")]:
        r = getattr(client, method)(path)
        assert r.status_code == 401, (path, r.status_code)


def test_ticket_is_bound_to_one_job(alice, new_job, settings):
    a, b = new_job(alice), new_job(alice)
    complete_with_file(settings, a["id"], data=b"A" * 100)
    complete_with_file(settings, b["id"], data=b"B" * 100)
    url = alice.post(f"/api/jobs/{a['id']}/download-ticket").json()["url"]
    r = alice.http.get(url)
    assert r.status_code == 200 and r.content == b"A" * 100
    assert set(r.content) == {ord("A")}


def test_deleted_users_jobs_cascade(alice, new_job, db):
    from sqlalchemy import func, select, delete

    from app.models import Job, User

    new_job(alice)
    db.execute(delete(User).where(User.id == uuid.UUID(alice.user_id)))
    db.commit()
    assert db.scalar(select(func.count()).select_from(Job)) == 0
