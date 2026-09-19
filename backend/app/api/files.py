"""Authenticated and ticketed file delivery with HTTP Range support."""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from .. import ratelimit
from ..config import Settings
from ..db import session_scope
from ..models import DownloadTicket, Job, Transfer
from ..schemas import TicketOut, TransferReport
from ..security import new_secret_token, sha256_hex, utcnow
from ..services import jobs as jobsvc
from ..services import storage
from ..services.errors import Conflict, NotFound
from ..states import JobStatus
from .deps import AuthDep, SessionDep, SettingsDep, api_error

router = APIRouter(tags=["files"])
log = logging.getLogger("ytaria.files")
CHUNK = 1024 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """Return (start, end) inclusive, ``None`` to serve the full body, or ``"invalid"`` for 416."""
    if not header:
        return None
    m = _RANGE.match(header.strip())
    if not m:
        return None  # unsupported syntax (e.g. multiple ranges): RFC 9110 lets us ignore it
    first, last = m.groups()
    if first == "" and last == "":
        return None
    if first == "":  # suffix: last N bytes
        n = int(last)
        if n == 0:
            return "invalid"
        start = max(0, size - n)
        return start, size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return "invalid"
    return start, min(end, size - 1)


def _etag(job: Job, st: os.stat_result) -> str:
    return '"' + sha256_hex(f"{job.id}:{st.st_size}:{st.st_mtime_ns}")[:32] + '"'


def _track_start(session, settings: Settings, job: Job, client_kind: str, is_range_continuation: bool) -> uuid.UUID:
    cutoff = utcnow() - timedelta(seconds=settings.transfer_active_window_seconds)
    transfer = None
    if is_range_continuation:  # ranged requests continue the same logical transfer
        transfer = session.execute(
            select(Transfer).where(Transfer.job_id == job.id, Transfer.user_id == job.user_id, Transfer.server_completed_at.is_(None),
                                   Transfer.last_seen_at > cutoff).order_by(Transfer.started_at.desc()).limit(1).with_for_update()
        ).scalar_one_or_none()
    if transfer is None:
        transfer = Transfer(job_id=job.id, user_id=job.user_id, client_kind=client_kind)
        session.add(transfer)
    transfer.last_seen_at = utcnow()
    session.commit()
    return transfer.id


def _stream(fd: int, start: int, length: int, transfer_id: uuid.UUID, total_size: int, range_end: int):
    sent = 0
    last_flush = 0
    try:
        os.lseek(fd, start, os.SEEK_SET)
        remaining = length
        while remaining > 0:
            chunk = os.read(fd, min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            sent += len(chunk)
            yield chunk
            if sent - last_flush >= 32 * CHUNK:
                _flush(transfer_id, sent - last_flush, False)
                last_flush = sent
    finally:
        os.close(fd)
        _flush(transfer_id, sent - last_flush, sent == length and range_end == total_size - 1)


def _flush(transfer_id: uuid.UUID, delta: int, finished: bool) -> None:
    try:
        with session_scope() as s:
            t = s.get(Transfer, transfer_id)
            if t is None:
                return
            t.bytes_sent += delta
            t.last_seen_at = utcnow()
            if finished:
                t.server_completed_at = utcnow()
    except Exception:  # accounting must never break a transfer
        log.warning("transfer accounting failed")


def serve_job_file(request: Request, session, settings: Settings, job: Job, client_kind: str) -> Response:
    if job.status != JobStatus.COMPLETED.value or not job.file_relpath or job.deleted_at is not None:
        raise api_error(409, "not_ready", "This download is not ready to transfer.")
    try:
        fd, st = storage.open_stored_file(settings, job.file_relpath)
    except (storage.UnsafePath, FileNotFoundError, OSError):
        log.error("stored file missing or unsafe", extra={"job_id": str(job.id)})
        raise api_error(410, "file_gone", "The file is no longer available on the server.") from None
    size = st.st_size
    etag = _etag(job, st)
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Disposition": storage.content_disposition(job.file_name or "download"),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    rng = parse_range(request.headers.get("range"), size)
    if_range = request.headers.get("if-range")
    if if_range and if_range.strip() != etag:
        rng = None  # validator mismatch: send the whole (new) representation
    if rng == "invalid":
        os.close(fd)
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", **headers})
    if request.method == "HEAD":
        os.close(fd)
        return Response(status_code=200, headers={**headers, "Content-Length": str(size)}, media_type=job.file_mime)
    if rng is None:
        start, end, status = 0, size - 1, 200
    else:
        start, end = rng  # type: ignore[misc]
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = end - start + 1 if size else 0
    headers["Content-Length"] = str(length)
    transfer_id = _track_start(session, settings, job, client_kind, is_range_continuation=(rng is not None and start > 0))
    return StreamingResponse(_stream(fd, start, length, transfer_id, size, end), status_code=status, headers=headers,
                             media_type=job.file_mime or "application/octet-stream")


def _client_kind(request: Request) -> str:
    return "native" if request.headers.get("x-client", "").lower() == "native" else "web"


@router.api_route("/jobs/{job_id}/file", methods=["GET", "HEAD"])
def download_file(job_id: uuid.UUID, request: Request, ctx: AuthDep, session: SessionDep, settings: SettingsDep) -> Response:
    job = jobsvc.get_job(session, ctx.user.id, job_id)
    return serve_job_file(request, session, settings, job, _client_kind(request))


@router.post("/jobs/{job_id}/download-ticket", response_model=TicketOut)
def create_ticket(job_id: uuid.UUID, ctx: AuthDep, session: SessionDep, settings: SettingsDep) -> TicketOut:
    """Issue a short-lived, single-file credential (for clients that cannot attach auth headers)."""
    ratelimit.check("ticket", str(ctx.user.id), settings.rl_ticket_user)
    job = jobsvc.get_job(session, ctx.user.id, job_id)
    if job.status != JobStatus.COMPLETED.value or not job.file_relpath:
        raise Conflict("not_ready", "This download is not ready to transfer.")
    token = new_secret_token()
    expires = utcnow() + timedelta(seconds=settings.download_ticket_ttl_seconds)
    session.add(DownloadTicket(token_hash=sha256_hex(token), job_id=job.id, user_id=ctx.user.id, expires_at=expires))
    session.commit()
    return TicketOut(url=f"/api/downloads/{token}", expires_at=expires)


@router.api_route("/downloads/{token}", methods=["GET", "HEAD"])
def download_with_ticket(token: str, request: Request, session: SessionDep, settings: SettingsDep) -> Response:
    """The ticket is the credential; it is valid for several requests until it expires so that
    Range/resume requests work. It is bound to one job and one user."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,64}", token):
        raise NotFound("Download link is invalid or expired.")
    ticket = session.execute(select(DownloadTicket).where(DownloadTicket.token_hash == sha256_hex(token))).scalar_one_or_none()
    if ticket is None or ticket.expires_at < utcnow():
        raise NotFound("Download link is invalid or expired.")
    job = session.execute(select(Job).where(Job.id == ticket.job_id, Job.user_id == ticket.user_id, Job.deleted_at.is_(None))).scalar_one_or_none()
    if job is None:
        raise NotFound("Download link is invalid or expired.")
    return serve_job_file(request, session, settings, job, _client_kind(request))


@router.post("/jobs/{job_id}/transfer", status_code=204)
def report_transfer(job_id: uuid.UUID, body: TransferReport, ctx: AuthDep, session: SessionDep) -> Response:
    """Client reports the outcome of saving the file on the device. Informational only."""
    job = jobsvc.get_job(session, ctx.user.id, job_id)
    transfer = session.execute(
        select(Transfer).where(Transfer.job_id == job.id, Transfer.user_id == ctx.user.id).order_by(Transfer.started_at.desc()).limit(1).with_for_update()
    ).scalar_one_or_none()
    if transfer is None:
        transfer = Transfer(job_id=job.id, user_id=ctx.user.id, client_kind="native")
        session.add(transfer)
    now = utcnow()
    transfer.last_seen_at = now
    if body.state == "saved":
        transfer.device_saved_at, transfer.device_failed_at = now, None
    else:
        transfer.device_failed_at = now
    session.commit()
    return Response(status_code=204)
