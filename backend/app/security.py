"""Password hashing, access tokens, refresh/ticket secrets and CSRF tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .config import Settings

_hasher = PasswordHasher()  # argon2id with library defaults (RFC 9106 low-memory profile)
# Verified against when the account does not exist so timing does not reveal registered emails.
_DUMMY_HASH = _hasher.hash("ytaria-dummy-password")

JWT_ALG = "HS256"
JWT_ISS = "ytaria-manager"
JWT_AUD = "ytaria-api"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret_token() -> str:
    """256 bits of entropy, URL-safe. Used for refresh tokens and download tickets."""
    return secrets.token_urlsafe(32)


def create_access_token(settings: Settings, user_id: uuid.UUID, family_id: uuid.UUID, now: datetime | None = None) -> str:
    now = now or utcnow()
    payload = {
        "iss": JWT_ISS,
        "aud": JWT_AUD,
        "sub": str(user_id),
        "sid": str(family_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.access_ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALG)


class InvalidToken(Exception):
    pass


def decode_access_token(settings: Settings, token: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[JWT_ALG],  # pinned: never trust the header's alg
            audience=JWT_AUD,
            issuer=JWT_ISS,
            options={"require": ["exp", "iat", "sub", "sid", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
    if claims.get("typ") != "access":
        raise InvalidToken("wrong token type")
    return claims


def csrf_token_for(settings: Settings, family_id: uuid.UUID | str) -> str:
    """CSRF token bound to the login session, so a token planted by an attacker cannot match."""
    mac = hmac.new(settings.secret_key.encode(), f"csrf:{family_id}".encode(), hashlib.sha256)
    return mac.hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
