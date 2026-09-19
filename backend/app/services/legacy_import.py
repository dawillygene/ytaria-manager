"""Optional, explicit import of jobs from the legacy single-user SQLite database.

Safety properties (each covered by tests):

* the source database is backed up first (real runs) and only ever opened read-only;
* ``dry_run`` performs no writes and copies no files;
* an explicit owner is required;
* repeated imports are no-ops (``legacy_imports`` has a unique (source, legacy id) key);
* only finished downloads whose files exist under ``files_root`` are imported as *completed*; files are
  *copied* (the originals are never moved or deleted); symlinks and paths outside ``files_root`` are refused;
* URLs must pass the current source policy;
* legacy ``command`` strings, ``cookies_browser`` and raw error text are never imported;
* unfinished jobs (pending/running/paused) are not resurrected into the new queue.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Job, JobEvent, LegacyImport, User
from ..security import utcnow
from . import storage
from .jobs import user_disk_usage, user_quota
from .urlpolicy import UrlRejected, validate_source_url


@dataclass
class ImportReport:
    source_id: str
    backup: Path | None = None
    imported: list[str] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"source id: {self.source_id}"]
        if self.backup:
            lines.append(f"backup: {self.backup}")
        lines.append(f"imported: {len(self.imported)}")
        lines += [f"  job {i}" for i in self.imported]
        lines.append(f"skipped: {len(self.skipped)}")
        lines += [f"  legacy #{i}: {why}" for i, why in self.skipped]
        return "\n".join(lines)


def source_id_for(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


def backup_sqlite(src: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    dest = src.with_name(f"{src.name}.bak-{stamp}")
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    dest.chmod(0o600)
    return dest


def _validate_file(files_root: Path, destination: str, output_dir: str) -> Path:
    if not destination:
        raise ValueError("no destination recorded")
    raw = Path(destination)
    candidate = raw if raw.is_absolute() else Path(output_dir) / raw
    if candidate.is_symlink():
        raise ValueError("file is a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("file not found") from None
    root = files_root.resolve()
    if root not in resolved.parents:
        raise ValueError("file is outside the allowed files root")
    if not resolved.is_file():
        raise ValueError("not a regular file")
    ext = resolved.suffix.lstrip(".").lower()
    if ext not in storage.ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported file type .{ext}")
    if resolved.stat().st_size == 0:
        raise ValueError("file is empty")
    return resolved


def import_legacy(
    session: Session,
    settings: Settings,
    *,
    sqlite_path: Path,
    owner_email: str,
    files_root: Path,
    dry_run: bool,
) -> ImportReport:
    sqlite_path = sqlite_path.resolve()
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"legacy database not found: {sqlite_path}")
    owner = session.execute(select(User).where(User.email == owner_email.strip().lower())).scalar_one_or_none()
    if owner is None:
        raise LookupError(f"owner {owner_email!r} does not exist; create the user first (python -m app.cli create-user)")
    report = ImportReport(source_id=source_id_for(sqlite_path))
    if not dry_run:
        report.backup = backup_sqlite(sqlite_path)

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, url, title, output_dir, status, destination, created_at, finished_at FROM jobs ORDER BY id").fetchall()
    finally:
        conn.close()

    used = user_disk_usage(session, owner.id)
    quota = user_quota(settings, owner)
    for row in rows:
        legacy_id = int(row["id"])
        already = session.execute(select(LegacyImport.id).where(LegacyImport.source_id == report.source_id, LegacyImport.legacy_job_id == legacy_id)).first()
        if already:
            report.skipped.append((legacy_id, "already imported"))
            continue
        status = str(row["status"])
        if status not in ("done", "failed", "canceled"):
            report.skipped.append((legacy_id, f"unfinished job ({status}) is not imported"))
            continue
        try:
            url = validate_source_url(str(row["url"]), allowed_hosts=settings.allowed_source_hosts, allowed_ports=settings.allowed_ports)
        except UrlRejected as exc:
            report.skipped.append((legacy_id, f"url rejected: {exc.code}"))
            continue
        source_file: Path | None = None
        if status == "done":
            try:
                source_file = _validate_file(files_root, str(row["destination"] or ""), str(row["output_dir"] or ""))
            except ValueError as exc:
                report.skipped.append((legacy_id, f"file check failed: {exc}"))
                continue
            size = source_file.stat().st_size
            if used + size > quota:
                report.skipped.append((legacy_id, "would exceed the owner's storage quota"))
                continue
            used += size
        if dry_run:
            report.imported.append(f"legacy #{legacy_id} ({status}) -> would import")
            continue

        job_id = uuid.uuid4()
        now = utcnow()
        job = Job(
            id=job_id, user_id=owner.id, url=url.url, url_hash=url.url_hash, host=urlsplit(url.url).hostname or "", selection="best",
            title=(str(row["title"] or "")[:300] or None), max_attempts=settings.max_attempts, attempts=0,
        )
        if status == "done" and source_file is not None:
            ext = source_file.suffix.lstrip(".").lower()
            dest_dir = storage.final_dir(settings, job_id)
            dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest = dest_dir / f"media.{ext}"
            shutil.copy2(source_file, dest)  # copy: the original is never moved or deleted
            dest.chmod(0o600)
            size = dest.stat().st_size
            job.status, job.progress_percent = "completed", 100.0
            job.file_relpath = dest.relative_to(storage.media_root(settings)).as_posix()
            job.file_name = storage.sanitize_filename(job.title, ext)
            job.file_size = job.total_bytes = job.downloaded_bytes = job.disk_bytes = size
            job.file_mime = storage.mime_for_ext(ext)
            job.finished_at = now
            job.expires_at = now + timedelta(hours=settings.completed_retention_hours)
        elif status == "failed":
            job.status, job.finished_at = "failed", now
            job.error_code, job.error_message = "legacy_failed", "This download failed before it was imported."
        else:
            job.status, job.finished_at = "canceled", now
        session.add(job)
        session.flush()
        session.add(JobEvent(job_id=job.id, user_id=owner.id, kind="info", message=f"Imported from legacy database (job #{legacy_id})"))
        session.add(LegacyImport(source_id=report.source_id, legacy_job_id=legacy_id, job_id=job.id))
        session.flush()
        report.imported.append(str(job.id))
    return report
