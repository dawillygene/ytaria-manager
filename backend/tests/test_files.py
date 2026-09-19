import os
import uuid

import pytest

from app.db import session_scope
from app.models import Job, Transfer
from app.services import storage
from tests.helpers import complete_with_file, get

DATA = bytes(range(256)) * 40  # 10240 bytes with a recognisable pattern


@pytest.fixture
def ready(alice, new_job, settings):
    job = new_job(alice)
    complete_with_file(settings, job["id"], data=DATA, title='Ünï "Clip"/..\\x')
    return job["id"]


def test_full_download_headers_and_body(alice, ready):
    r = alice.get(f"/api/jobs/{ready}/file")
    assert r.status_code == 200 and r.content == DATA
    assert r.headers["content-type"] == "video/mp4" and r.headers["content-length"] == str(len(DATA))
    assert r.headers["accept-ranges"] == "bytes" and r.headers["x-content-type-options"] == "nosniff"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;") and "/" not in cd.split("filename=")[1].split(";")[0].strip('"') and "\\" not in cd
    assert r.headers["cache-control"] == "private, no-store"


def test_range_requests(alice, ready):
    url = f"/api/jobs/{ready}/file"
    r = alice.get(url, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == DATA[:10] and r.headers["content-range"] == f"bytes 0-9/{len(DATA)}"
    r = alice.get(url, headers={"Range": "bytes=10000-"})
    assert r.status_code == 206 and r.content == DATA[10000:]
    r = alice.get(url, headers={"Range": "bytes=-5"})
    assert r.status_code == 206 and r.content == DATA[-5:]
    r = alice.get(url, headers={"Range": f"bytes=5-{len(DATA) * 2}"})
    assert r.status_code == 206 and r.content == DATA[5:] and r.headers["content-range"] == f"bytes 5-{len(DATA) - 1}/{len(DATA)}"
    r = alice.get(url, headers={"Range": f"bytes={len(DATA)}-"})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(DATA)}"
    assert alice.get(url, headers={"Range": "bytes=9-3"}).status_code == 416
    assert alice.get(url, headers={"Range": "bytes=0-1,5-6"}).status_code == 200      # multi-range ignored per RFC 9110
    assert alice.get(url, headers={"Range": "garbage"}).status_code == 200


def test_if_range_validator(alice, ready):
    url = f"/api/jobs/{ready}/file"
    etag = alice.get(url).headers["etag"]
    assert alice.get(url, headers={"Range": "bytes=0-4", "If-Range": etag}).status_code == 206
    r = alice.get(url, headers={"Range": "bytes=0-4", "If-Range": '"stale"'})
    assert r.status_code == 200 and r.content == DATA


def test_head_and_resume_after_interruption(alice, ready):
    url = f"/api/jobs/{ready}/file"
    h = alice.http.head(url)
    assert h.status_code == 200 and h.headers["content-length"] == str(len(DATA)) and h.content == b""
    first = alice.get(url, headers={"Range": "bytes=0-4999"}).content      # connection "dropped" after 5000 bytes
    rest = alice.get(url, headers={"Range": "bytes=5000-"}).content
    assert first + rest == DATA


def test_delivery_state_is_separate_from_server_state(alice, ready):
    assert alice.get(f"/api/jobs/{ready}").json()["delivery"]["state"] == "ready"
    alice.get(f"/api/jobs/{ready}/file")
    j = alice.get(f"/api/jobs/{ready}").json()
    assert j["status"] == "completed" and j["delivery"]["state"] == "sent"       # streamed, but not confirmed by the device
    assert alice.post(f"/api/jobs/{ready}/transfer", {"state": "saved"}).status_code == 204
    j = alice.get(f"/api/jobs/{ready}").json()
    assert j["delivery"]["state"] == "saved" and j["delivery"]["saved_at"]
    with session_scope() as s:
        t = s.query(Transfer).one()
        assert t.bytes_sent == len(DATA) and t.server_completed_at is not None


def test_partial_range_does_not_mark_sent(alice, ready):
    alice.get(f"/api/jobs/{ready}/file", headers={"Range": "bytes=0-99"})
    assert alice.get(f"/api/jobs/{ready}").json()["delivery"]["state"] == "transferring"


def test_ticket_flow(alice, client, ready, settings, monkeypatch, caplog):
    t = alice.post(f"/api/jobs/{ready}/download-ticket").json()
    assert t["url"].startswith("/api/downloads/") and len(t["url"]) > 40
    token = t["url"].rsplit("/", 1)[1]
    r = client.get(t["url"])                          # no cookies, no headers: the ticket is the credential
    assert r.status_code == 200 and r.content == DATA
    assert client.get(t["url"], headers={"Range": "bytes=1-3"}).content == DATA[1:4]   # multi-use for resume
    assert token not in caplog.text
    with session_scope() as s:                        # expiry
        from datetime import timedelta
        from app.models import DownloadTicket
        from app.security import utcnow

        s.query(DownloadTicket).update({"expires_at": utcnow() - timedelta(seconds=1)})
    assert client.get(t["url"]).status_code == 404
    assert client.get("/api/downloads/" + "A" * 43).status_code == 404
    assert client.get("/api/downloads/short").status_code == 404
    with session_scope() as s:
        assert s.query(DownloadTicket).count() == 1 and token not in str([x.token_hash for x in s.query(DownloadTicket)])  # only a hash is stored


def test_ticket_dies_with_the_job(alice, client, ready, db):
    t = alice.post(f"/api/jobs/{ready}/download-ticket").json()
    with session_scope() as s:
        s.query(Job).update({"status": "expired", "file_relpath": None})
    assert client.get(t["url"]).status_code == 409
    assert alice.post(f"/api/jobs/{ready}/download-ticket").status_code == 409


def test_not_ready_and_gone(alice, new_job, ready, settings):
    queued = new_job(alice)
    assert alice.get(f"/api/jobs/{queued['id']}/file").status_code == 409
    j = get(ready)
    os.unlink(storage.media_root(settings) / j.file_relpath)
    r = alice.get(f"/api/jobs/{ready}/file")
    assert r.status_code == 410 and r.json()["error"]["code"] == "file_gone"


def test_symlink_swapped_in_is_not_served(alice, ready, settings, tmp_path):
    j = get(ready)
    target = storage.media_root(settings) / j.file_relpath
    secret = tmp_path / "secret"
    secret.write_text("hunter2")
    target.unlink()
    target.symlink_to(secret)
    r = alice.get(f"/api/jobs/{ready}/file")
    assert r.status_code == 410 and b"hunter2" not in r.content


def test_db_path_traversal_value_is_not_served(alice, ready, settings, tmp_path):
    (tmp_path / "x.mp4").write_bytes(b"outside")
    with session_scope() as s:
        s.query(Job).update({"file_relpath": "../../../../../../" + str(tmp_path / "x.mp4").lstrip("/")})
    r = alice.get(f"/api/jobs/{ready}/file")
    assert r.status_code == 410 and b"outside" not in r.content


def test_only_a_hash_of_ticket_and_no_paths_in_api(alice, ready, settings):
    body = alice.get(f"/api/jobs/{ready}").text
    assert str(settings.media_root) not in body and "final/media" not in body and "jobs/" not in body
