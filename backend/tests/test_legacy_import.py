import sqlite3
from pathlib import Path

import pytest

from app import cli
from app.db import session_scope
from app.models import Job, LegacyImport, User
from app.services import storage
from app.services.legacy_import import import_legacy


@pytest.fixture
def legacy(tmp_path):
    files = tmp_path / "downloads"
    files.mkdir()
    (files / "good-[abc].mp4").write_bytes(b"v" * 300)
    (tmp_path / "outside.mp4").write_bytes(b"secret")
    (files / "link.mp4").symlink_to(tmp_path / "outside.mp4")
    (files / "notes.txt").write_text("hi")
    db = tmp_path / "jobs.sqlite3"
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE jobs (id INTEGER PRIMARY KEY, url TEXT, title TEXT, output_dir TEXT, command TEXT, status TEXT, progress REAL,
                 speed TEXT, eta TEXT, destination TEXT, error TEXT, cookies_browser TEXT, created_at TEXT, started_at TEXT, finished_at TEXT)""")
    rows = [
        (1, "https://www.youtube.com/watch?v=abc", "Good", str(files), "yt-dlp --cookies-from-browser firefox SECRET", "done", 100, "", "", "good-[abc].mp4", "", "firefox"),
        (2, "https://www.youtube.com/watch?v=def", "Missing", str(files), "cmd", "done", 100, "", "", "gone.mp4", "", ""),
        (3, "https://www.youtube.com/watch?v=ghi", "Symlink", str(files), "cmd", "done", 100, "", "", "link.mp4", "", ""),
        (4, "https://www.youtube.com/watch?v=jkl", "Traversal", str(files), "cmd", "done", 100, "", "", "../outside.mp4", "", ""),
        (5, "https://www.youtube.com/watch?v=mno", "Failed", str(files), "cmd", "failed", 0, "", "", "", "ERROR: /home/me/.config secret path", ""),
        (6, "https://www.youtube.com/watch?v=pqr", "Pending", str(files), "cmd", "pending", 0, "", "", "", "", ""),
        (7, "http://169.254.169.254/x", "SSRF", str(files), "cmd", "failed", 0, "", "", "", "", ""),
        (8, "https://www.youtube.com/watch?v=stu", "Txt", str(files), "cmd", "done", 100, "", "", "notes.txt", "", ""),
    ]
    for r in rows:
        c.execute("INSERT INTO jobs (id,url,title,output_dir,command,status,progress,speed,eta,destination,error,cookies_browser,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'2024-01-01')", r)
    c.commit()
    c.close()
    return db, files


@pytest.fixture
def owner(settings):
    with session_scope() as s:
        s.add(User(email="owner@example.com", display_name="o", password_hash="x"))


def run(settings, db, files, dry):
    with session_scope() as s:
        return import_legacy(s, settings, sqlite_path=db, owner_email="owner@example.com", files_root=files, dry_run=dry)


def test_dry_run_writes_nothing(settings, legacy, owner):
    db, files = legacy
    before = db.read_bytes()
    report = run(settings, db, files, dry=True)
    assert len(report.imported) == 2 and report.backup is None
    assert list(db.parent.glob("*.bak-*")) == []
    with session_scope() as s:
        assert s.query(Job).count() == 0 and s.query(LegacyImport).count() == 0
    assert db.read_bytes() == before
    assert not (storage.media_root(settings) / "jobs").exists() or not list((storage.media_root(settings) / "jobs").iterdir())


def test_import_validates_copies_and_is_repeatable(settings, legacy, owner):
    db, files = legacy
    report = run(settings, db, files, dry=False)
    assert report.backup and report.backup.exists() and report.backup.stat().st_mode & 0o077 == 0
    reasons = dict(report.skipped)
    assert "file not found" in reasons[2] and "symlink" in reasons[3] and "outside" in reasons[4]
    assert "unfinished" in reasons[6] and "url rejected" in reasons[7] and "unsupported file type" in reasons[8]
    with session_scope() as s:
        jobs = {j.title: j for j in s.query(Job).all()}
        assert set(jobs) == {"Good", "Failed"}
        good = jobs["Good"]
        assert good.status == "completed" and good.file_size == 300 and (storage.media_root(settings) / good.file_relpath).read_bytes() == b"v" * 300
        assert jobs["Failed"].status == "failed" and "secret path" not in (jobs["Failed"].error_message or "") and jobs["Failed"].debug_tail is None
        blob = " ".join(str(getattr(good, c.name)) for c in Job.__table__.columns)
        assert "cookies" not in blob and "SECRET" not in blob and "yt-dlp" not in blob
    assert (files / "good-[abc].mp4").exists()                                   # original untouched
    again = run(settings, db, files, dry=False)                                   # repeated import: no duplicates
    assert again.imported == [] and dict(again.skipped)[1] == "already imported"
    with session_scope() as s:
        assert s.query(Job).count() == 2


def test_owner_is_required_and_must_exist(settings, legacy):
    db, files = legacy
    with pytest.raises(LookupError):
        run(settings, db, files, dry=False)
    assert cli.main is not None
