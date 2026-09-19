from __future__ import annotations

import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .. import ratelimit
from ..config import Settings
from ..models import RefreshToken, User
from ..schemas import AuthOut, LoginIn, RefreshIn, RegisterIn, UserOut
from ..security import (
    constant_time_equal,
    create_access_token,
    csrf_token_for,
    hash_password,
    new_secret_token,
    password_needs_rehash,
    sha256_hex,
    utcnow,
    verify_password,
)
from .deps import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    AuthDep,
    SessionDep,
    SettingsDep,
    api_error,
    check_origin,
    client_ip,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _is_native(request: Request) -> bool:
    return request.headers.get("x-client", "").lower() == "native"


def _normalise_email(raw: str) -> str:
    email = raw.strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise api_error(422, "invalid_email", "Enter a valid email address.")
    return email


def _validate_password(settings: Settings, password: str, email: str) -> None:
    if len(password) < settings.password_min_length:
        raise api_error(422, "weak_password", f"Use at least {settings.password_min_length} characters.")
    if password.lower() == email or password.strip() == "":
        raise api_error(422, "weak_password", "Choose a stronger password.")


def _issue_tokens(session, settings: Settings, user: User, request: Request, family_id: uuid.UUID | None = None) -> tuple[str, str, uuid.UUID]:
    family_id = family_id or uuid.uuid4()
    refresh = new_secret_token()
    session.add(RefreshToken(
        user_id=user.id, family_id=family_id, token_hash=sha256_hex(refresh),
        client_kind="native" if _is_native(request) else "web",
        user_agent=request.headers.get("user-agent", "")[:200],
        expires_at=utcnow() + timedelta(seconds=settings.refresh_ttl_seconds),
    ))
    session.flush()
    return create_access_token(settings, user.id, family_id), refresh, family_id


def _respond(response: Response, request: Request, settings: Settings, user: User, access: str, refresh: str, family_id: uuid.UUID) -> AuthOut:
    csrf = csrf_token_for(settings, family_id)
    if _is_native(request):
        response.headers["Cache-Control"] = "no-store"
        return AuthOut(user=UserOut.model_validate(user), access_token=access, refresh_token=refresh, token_type="Bearer",
                       expires_in=settings.access_ttl_seconds)
    common = dict(httponly=True, secure=settings.cookie_secure, samesite=settings.cookie_samesite)
    response.set_cookie(ACCESS_COOKIE, access, max_age=settings.access_ttl_seconds, path="/api", **common)
    response.set_cookie(REFRESH_COOKIE, refresh, max_age=settings.refresh_ttl_seconds, path="/api/auth", **common)
    return AuthOut(user=UserOut.model_validate(user), csrf_token=csrf)


def _clear_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/api", secure=settings.cookie_secure, httponly=True, samesite=settings.cookie_samesite)
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth", secure=settings.cookie_secure, httponly=True, samesite=settings.cookie_samesite)


@router.post("/register", response_model=AuthOut, status_code=201)
def register(body: RegisterIn, request: Request, response: Response, session: SessionDep, settings: SettingsDep) -> AuthOut:
    check_origin(request, settings)
    ratelimit.check("register", client_ip(request), settings.rl_register_ip)
    if settings.registration_mode == "closed":
        raise api_error(403, "registration_closed", "Registration is closed.")
    if settings.registration_mode == "invite" and not (
        settings.registration_code and constant_time_equal(body.invite_code, settings.registration_code)
    ):
        raise api_error(403, "invalid_invite", "A valid invite code is required.")
    email = _normalise_email(body.email)
    _validate_password(settings, body.password, email)
    user = User(email=email, display_name=body.display_name or email.split("@")[0], password_hash=hash_password(body.password))
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise api_error(409, "email_taken", "An account with this email already exists.") from None
    access, refresh, family = _issue_tokens(session, settings, user, request)
    session.commit()
    return _respond(response, request, settings, user, access, refresh, family)


@router.post("/login", response_model=AuthOut)
def login(body: LoginIn, request: Request, response: Response, session: SessionDep, settings: SettingsDep) -> AuthOut:
    check_origin(request, settings)
    email = body.email.strip().lower()
    ratelimit.check("login-ip", client_ip(request), settings.rl_login_ip)
    ratelimit.check("login-acct", sha256_hex(email), settings.rl_login_account)
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    ok = verify_password(body.password, user.password_hash if user else None)
    if not ok or user is None or not user.is_active:
        raise api_error(401, "invalid_credentials", "Incorrect email or password.")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    access, refresh, family = _issue_tokens(session, settings, user, request)
    session.commit()
    return _respond(response, request, settings, user, access, refresh, family)


@router.post("/refresh", response_model=AuthOut)
def refresh(body: RefreshIn, request: Request, response: Response, session: SessionDep, settings: SettingsDep) -> AuthOut:
    """Rotate the refresh token. A rotated token presented again revokes the whole session family."""
    native = _is_native(request)
    if not native:
        check_origin(request, settings)
        # Cookie-authenticated: require a header a cross-site form/simple request cannot send.
        if request.headers.get("x-requested-with") != "ytaria-web":
            raise api_error(403, "csrf_failed", "Missing request header.")
    ratelimit.check("refresh", client_ip(request), settings.rl_refresh_ip)
    presented = body.refresh_token if native else request.cookies.get(REFRESH_COOKIE)
    if not presented:
        raise api_error(401, "not_authenticated", "Sign in to continue.")
    row = session.execute(select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(presented)).with_for_update()).scalar_one_or_none()
    now = utcnow()
    if row is None or row.revoked_at is not None or row.expires_at < now:
        raise api_error(401, "invalid_token", "Your session has expired. Sign in again.")
    if row.used_at is not None:
        if (now - row.used_at).total_seconds() <= settings.refresh_reuse_grace_seconds:
            # Two tabs racing to refresh: not an attack. Let the caller retry with the newer cookie.
            raise api_error(401, "refresh_conflict", "Session refresh in progress. Retry.")
        session.execute(update(RefreshToken).where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now))
        session.commit()
        raise api_error(401, "refresh_reuse", "Your session was ended for security. Sign in again.")
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise api_error(401, "invalid_token", "Your session has expired. Sign in again.")
    row.used_at = now
    access, new_refresh, family = _issue_tokens(session, settings, user, request, family_id=row.family_id)
    session.commit()
    return _respond(response, request, settings, user, access, new_refresh, family)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, session: SessionDep, settings: SettingsDep, body: RefreshIn | None = None) -> Response:
    body = body or RefreshIn()
    native = _is_native(request)
    if not native:
        check_origin(request, settings)
    presented = body.refresh_token if native else request.cookies.get(REFRESH_COOKIE)
    if presented:
        row = session.execute(select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(presented))).scalar_one_or_none()
        if row is not None:
            session.execute(update(RefreshToken).where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
            session.commit()
    _clear_cookies(response, settings)
    response.status_code = 204
    return response


@router.get("/me", response_model=AuthOut)
def me(ctx: AuthDep, settings: SettingsDep) -> AuthOut:
    return AuthOut(user=UserOut.model_validate(ctx.user), csrf_token=csrf_token_for(settings, ctx.family_id) if ctx.via == "cookie" else None)
