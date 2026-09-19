from __future__ import annotations

import os
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

_ADMIN_DSN = os.environ.get("YTARIA_TEST_ADMIN_DSN", "postgresql://ytaria:dev@127.0.0.1:55432/postgres")
_TEST_DB = "ytaria_test"
_MEDIA = Path(tempfile.mkdtemp(prefix="ytaria-test-media-"))
_SCRATCH = Path(tempfile.mkdtemp(prefix="ytaria-test-scratch-"))

os.environ.update({
    "YTARIA_ENV": "test",
    "YTARIA_SECRET_KEY": "test-secret-key-that-is-long-enough-0123456789",
    "YTARIA_DATABASE_URL": f"postgresql+psycopg://ytaria:dev@127.0.0.1:55432/{_TEST_DB}",
    "YTARIA_RATELIMIT_URL": "memory://",
    "YTARIA_MEDIA_ROOT": str(_MEDIA),
    "YTARIA_SCRATCH_ROOT": str(_SCRATCH),
    "YTARIA_MIN_FREE_DISK_BYTES": "0",
    "YTARIA_ALLOW_DIRECT_EGRESS": "true",
    "YTARIA_PASSWORD_MIN_LENGTH": "10",
})


def _ensure_database() -> None:
    with psycopg.connect(_ADMIN_DSN, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (_TEST_DB,)).fetchone()
        if exists:
            conn.execute(f'DROP DATABASE "{_TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_TEST_DB}"')


@pytest.fixture(scope="session", autouse=True)
def _schema():
    _ensure_database()
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    command.upgrade(cfg, "head")  # also proves the migration applies from scratch
    yield


@pytest.fixture(autouse=True)
def _clean(_schema):
    from app.db import get_engine
    from app.ratelimit import get_backend

    with get_engine().begin() as conn:
        conn.exec_driver_sql(
            "TRUNCATE users, refresh_tokens, inspections, jobs, job_events, download_tickets, transfers, legacy_imports RESTART IDENTITY CASCADE"
        )
    backend = get_backend()
    if hasattr(backend, "clear"):
        backend.clear()
    import shutil

    for child in _MEDIA.iterdir():
        shutil.rmtree(child, ignore_errors=True)
    yield


class RecordingDispatcher:
    def __init__(self) -> None:
        self.downloads: list[uuid.UUID] = []
        self.inspections: list[uuid.UUID] = []
        self.fail = False

    def download(self, job_id):
        if self.fail:
            raise RuntimeError("broker down")
        self.downloads.append(job_id)

    def inspection(self, inspection_id):
        if self.fail:
            raise RuntimeError("broker down")
        self.inspections.append(inspection_id)


@pytest.fixture
def dispatcher():
    from app.worker.dispatch import get_dispatcher, set_dispatcher

    original = get_dispatcher()
    rec = RecordingDispatcher()
    set_dispatcher(rec)
    yield rec
    set_dispatcher(original)


@pytest.fixture
def settings():
    from app.config import get_settings

    return get_settings()


@pytest.fixture
def db():
    from app.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture
def client(dispatcher):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://testserver") as c:
        yield c


class Session:
    """A logged-in browser-style API session (cookie auth + CSRF header)."""

    def __init__(self, client, email: str, password: str = "correct horse battery") -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        self.http = TestClient(app, base_url="http://testserver")
        self.email, self.password = email, password
        r = self.http.post("/api/auth/register", json={"email": email, "password": password})
        assert r.status_code == 201, r.text
        self.user_id = r.json()["user"]["id"]
        self.csrf = r.json()["csrf_token"]

    def h(self, **extra):
        return {"X-CSRF-Token": self.csrf, **extra}

    def get(self, path, **kw):
        return self.http.get(path, **kw)

    def post(self, path, json=None, **kw):
        return self.http.post(path, json=json, headers=self.h(**kw.pop("headers", {})), **kw)

    def delete(self, path, **kw):
        return self.http.delete(path, headers=self.h(), **kw)


@pytest.fixture
def alice(client):
    return Session(client, "alice@example.com")


@pytest.fixture
def bob(client):
    return Session(client, "bob@example.com")


def make_inspection(user_id, url="https://www.youtube.com/watch?v=abc123", options=None, status="succeeded"):
    from app.db import session_scope
    from app.models import Inspection
    from app.security import sha256_hex, utcnow

    options = options or [
        {"id": "best", "label": "Best available (1080p)", "kind": "video", "height": 1080, "ext": "mp4", "estimated_bytes": 1000},
        {"id": "h720", "label": "720p", "kind": "video", "height": 720, "ext": "mp4", "estimated_bytes": 500},
        {"id": "audio_mp3", "label": "Audio (MP3)", "kind": "audio", "height": None, "ext": "mp3", "estimated_bytes": 100},
    ]
    with session_scope() as s:
        insp = Inspection(user_id=uuid.UUID(str(user_id)), url=url, url_hash=sha256_hex(url), status=status,
                          result={"title": "Test video", "duration_seconds": 60, "extractor": "Youtube", "options": options},
                          expires_at=utcnow() + timedelta(minutes=30))
        s.add(insp)
        s.flush()
        return insp.id


@pytest.fixture
def new_job(dispatcher):
    """Create a queued job for a session via the real API. Returns the JSON."""

    counter = {"n": 0}

    def _make(sess, selection="best", url=None, idem=None):
        counter["n"] += 1
        url = url or f"https://www.youtube.com/watch?v=vid{counter['n']:04d}"
        insp = make_inspection(sess.user_id, url=url)
        headers = {"Idempotency-Key": idem} if idem else {}
        r = sess.post("/api/jobs", {"inspection_id": str(insp), "selection": selection}, headers=headers)
        assert r.status_code in (200, 201), r.text
        return r.json()

    return _make
