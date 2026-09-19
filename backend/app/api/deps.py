from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_session
from ..models import RefreshToken, User
from ..security import InvalidToken, constant_time_equal, csrf_token_for, decode_access_token, utcnow

ACCESS_COOKIE = "ytaria_at"
REFRESH_COOKIE = "ytaria_rt"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]


def api_error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message}, headers=headers)


def client_ip(request: Request) -> str:
    """Peer address. Behind Nginx uvicorn rewrites this from X-Forwarded-For for trusted proxies only."""
    return request.client.host if request.client else "unknown"


def check_origin(request: Request, settings: Settings) -> None:
    """Reject unsafe requests whose Origin is neither this site nor an explicitly configured origin."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    if origin in settings.cors_origins:
        return
    host = request.headers.get("host", "")
    if urlsplit(origin).netloc == host and host:
        return
    raise api_error(403, "origin_not_allowed", "Cross-site request rejected.")


@dataclass
class AuthContext:
    user: User
    family_id: uuid.UUID
    via: str  # bearer | cookie


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token else None


def authenticate(request: Request, session: SessionDep, settings: SettingsDep) -> AuthContext:
    token = _bearer(request)
    via = "bearer"
    if token is None:
        token = request.cookies.get(ACCESS_COOKIE)
        via = "cookie"
    if not token:
        raise api_error(401, "not_authenticated", "Sign in to continue.")
    try:
        claims = decode_access_token(settings, token)
        user_id, family_id = uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
    except (InvalidToken, ValueError):
        raise api_error(401, "invalid_token", "Your session has expired. Sign in again.") from None

    if via == "cookie" and request.method not in SAFE_METHODS:
        check_origin(request, settings)
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied or not constant_time_equal(supplied, csrf_token_for(settings, family_id)):
            raise api_error(403, "csrf_failed", "Missing or invalid CSRF token.")

    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise api_error(401, "invalid_token", "Your session has expired. Sign in again.")
    # Logout / reuse-detection revokes the family; honour that immediately, not at access-token expiry.
    live = session.scalar(
        select(RefreshToken.id).where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None),
                                      RefreshToken.expires_at > utcnow()).limit(1)
    )
    if live is None:
        raise api_error(401, "session_revoked", "Your session has ended. Sign in again.")
    return AuthContext(user=user, family_id=family_id, via=via)


AuthDep = Annotated[AuthContext, Depends(authenticate)]


def current_user(ctx: AuthDep) -> User:
    return ctx.user


UserDep = Annotated[User, Depends(current_user)]
