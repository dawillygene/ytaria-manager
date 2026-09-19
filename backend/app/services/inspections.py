"""URL inspection: API-side creation and worker-side execution."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Inspection, User
from ..security import utcnow
from .engine import InspectionError, build_inspect_command, classify_failure, parse_inspection, safe_env
from .errors import ServiceError
from .urlpolicy import DestinationBlocked, ValidatedUrl, resolve_public

log = logging.getLogger("ytaria.inspect")
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def create_inspection(session: Session, settings: Settings, user: User, url: ValidatedUrl) -> tuple[Inspection, bool]:
    """Return ``(inspection, is_new)``. Reuses a live inspection of the same URL by the same user."""
    now = utcnow()
    session.execute(select(User.id).where(User.id == user.id).with_for_update())
    existing = session.execute(
        select(Inspection)
        .where(Inspection.user_id == user.id, Inspection.url_hash == url.url_hash, Inspection.expires_at > now,
               Inspection.status.in_(["pending", "running", "succeeded"]))
        .order_by(Inspection.created_at.desc())
    ).scalars().first()
    if existing:
        return existing, False
    pending = session.scalar(
        select(func.count()).select_from(Inspection).where(Inspection.user_id == user.id, Inspection.status.in_(["pending", "running"]))
    ) or 0
    if pending >= settings.max_pending_inspections_per_user:
        raise ServiceError("too_many_inspections", "Please wait for your current link checks to finish.", 429)
    insp = Inspection(user_id=user.id, url=url.url, url_hash=url.url_hash, status="pending",
                      expires_at=now + timedelta(seconds=settings.inspection_ttl_seconds))
    session.add(insp)
    session.flush()
    return insp, True


def _run_capped(cmd: list[str], env: dict[str, str], timeout: int) -> tuple[int, str, str]:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            start_new_session=True, close_fds=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        proc.communicate()
        raise
    finally:
        # Reap stragglers (aria2c/ffmpeg are not used for inspection, but never leave a group behind).
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if len(out) > MAX_OUTPUT_BYTES:
        raise InspectionError("metadata_too_large", "The link's metadata is too large to process.")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def execute_inspection(session: Session, settings: Settings, inspection_id: uuid.UUID) -> None:
    """Worker side. Idempotent: only ``pending`` inspections are claimed."""
    insp = session.execute(select(Inspection).where(Inspection.id == inspection_id).with_for_update()).scalar_one_or_none()
    if insp is None or insp.status != "pending" or insp.expires_at < utcnow():
        return
    insp.status = "running"
    insp.started_at = utcnow()
    session.commit()

    try:
        _preflight_destination(settings, insp.url)
        scratch = settings.scratch_root.resolve() / "inspect" / str(insp.id)
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        cmd = build_inspect_command(settings, insp.url, scratch / "cache")
        code, out, err = _run_capped(cmd, safe_env(settings, scratch), settings.inspect_timeout_seconds)
        if code != 0:
            failure = classify_failure(err.splitlines()[-20:])
            log.info("inspection failed code=%s class=%s", code, failure.code)
            raise InspectionError(failure.code, failure.message, failure.retryable)
        result = parse_inspection(out, settings).as_dict()
        insp.status, insp.result = "succeeded", result
    except subprocess.TimeoutExpired:
        insp.status, insp.error_code, insp.error_message = "failed", "timeout", "Checking this link took too long."
    except DestinationBlocked:
        insp.status, insp.error_code, insp.error_message = "failed", "blocked_destination", "That destination is not allowed."
    except InspectionError as exc:
        insp.status, insp.error_code, insp.error_message = "failed", exc.code, exc.message
    except Exception:  # pragma: no cover - unexpected; never leak details
        log.exception("inspection crashed")
        insp.status, insp.error_code, insp.error_message = "failed", "internal", "Something went wrong while checking this link."
    finally:
        insp.finished_at = utcnow()
        session.commit()
        try:
            import shutil

            shutil.rmtree(settings.scratch_root.resolve() / "inspect" / str(insp.id), ignore_errors=True)
        except Exception:  # pragma: no cover
            pass


def _preflight_destination(settings: Settings, url: str) -> None:
    """Best-effort resolved-address check for setups without an egress proxy (development only).

    With a proxy the proxy is the authority; the worker cannot resolve public DNS on the isolated
    network anyway.
    """
    if settings.egress_proxy_url:
        return
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    resolve_public(parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80), allowed_ports=settings.allowed_ports)
