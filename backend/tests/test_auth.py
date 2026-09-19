from datetime import timedelta

import pytest

from app.security import create_access_token, utcnow
from tests.conftest import Session


def test_register_login_logout_me(client):
    s = Session(client, "carol@example.com")
    me = s.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["user"]["email"] == "carol@example.com"
    assert me.json()["csrf_token"] == s.csrf
    # cookies are HttpOnly and scoped
    set_cookie = s.http.post("/api/auth/login", json={"email": "carol@example.com", "password": s.password}).headers.get_list("set-cookie")
    assert any("ytaria_at=" in c and "HttpOnly" in c and "Path=/api" in c for c in set_cookie)
    assert any("ytaria_rt=" in c and "HttpOnly" in c and "Path=/api/auth" in c for c in set_cookie)
    assert s.post("/api/auth/logout").status_code == 204
    assert s.get("/api/auth/me").status_code == 401


def test_logout_revokes_access_token_immediately(client):
    s = Session(client, "dave@example.com")
    access = s.http.cookies.get("ytaria_at")
    assert access
    assert s.post("/api/auth/logout").status_code == 204
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "session_revoked"


def test_wrong_password_and_unknown_user_look_identical(client):
    Session(client, "erin@example.com")
    a = client.post("/api/auth/login", json={"email": "erin@example.com", "password": "nope-nope-nope"})
    b = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_duplicate_email_and_weak_password(client):
    Session(client, "frank@example.com")
    assert client.post("/api/auth/register", json={"email": "FRANK@example.com", "password": "another long password"}).status_code == 409
    assert client.post("/api/auth/register", json={"email": "x@example.com", "password": "short"}).status_code == 422
    assert client.post("/api/auth/register", json={"email": "not-an-email", "password": "long enough password"}).status_code == 422


def test_access_token_expiry(client, settings, db):
    s = Session(client, "gina@example.com")
    import uuid

    from app.models import RefreshToken
    from sqlalchemy import select

    fam = db.scalar(select(RefreshToken.family_id))
    old = create_access_token(settings, uuid.UUID(s.user_id), fam, now=utcnow() - timedelta(seconds=settings.access_ttl_seconds + 60))
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


def test_forged_and_alg_none_tokens_rejected(client):
    import jwt

    forged = jwt.encode({"sub": "x", "sid": "y"}, "wrong-secret", algorithm="HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    none_tok = jwt.encode({"sub": "x"}, None, algorithm="none")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {none_tok}"}).status_code == 401


def test_csrf_required_for_cookie_auth_but_not_bearer(client):
    s = Session(client, "hank@example.com")
    r = s.http.post("/api/inspections", json={"url": "https://www.youtube.com/watch?v=abc"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = s.http.post("/api/inspections", json={"url": "https://www.youtube.com/watch?v=abc"}, headers={"X-CSRF-Token": "0" * 64})
    assert r.status_code == 403
    r = s.post("/api/inspections", {"url": "https://www.youtube.com/watch?v=abc"})
    assert r.status_code == 202


def test_cross_origin_rejected(client):
    s = Session(client, "iris@example.com")
    r = s.post("/api/inspections", {"url": "https://www.youtube.com/watch?v=abc"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "origin_not_allowed"
    r = client.post("/api/auth/login", json={"email": "iris@example.com", "password": s.password}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_refresh_rotation_and_reuse_detection(client, settings, db, monkeypatch):
    s = Session(client, "jack@example.com")
    old_rt = s.http.cookies.get("ytaria_rt")
    hdr = {"X-Requested-With": "ytaria-web"}
    r = s.http.post("/api/auth/refresh", json={}, headers=hdr)
    assert r.status_code == 200
    new_rt = s.http.cookies.get("ytaria_rt")
    assert new_rt != old_rt
    # replaying the old token right away is treated as a benign race (no revocation) ...
    s.http.cookies.set("ytaria_rt", old_rt, path="/api/auth")
    assert s.http.post("/api/auth/refresh", json={}, headers=hdr).json()["error"]["code"] == "refresh_conflict"
    # ... but after the grace window it revokes the whole family, including the newest token
    monkeypatch.setattr(type(settings), "refresh_reuse_grace_seconds", 0, raising=False)
    object.__setattr__(settings, "refresh_reuse_grace_seconds", 0)
    import time

    time.sleep(0.05)
    s.http.cookies.set("ytaria_rt", old_rt, path="/api/auth")
    assert s.http.post("/api/auth/refresh", json={}, headers=hdr).json()["error"]["code"] == "refresh_reuse"
    s.http.cookies.set("ytaria_rt", new_rt, path="/api/auth")
    assert s.http.post("/api/auth/refresh", json={}, headers=hdr).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {s.http.cookies.get('ytaria_at')}"}).status_code == 401


def test_refresh_requires_custom_header(client):
    s = Session(client, "kim@example.com")
    assert s.http.post("/api/auth/refresh", json={}).status_code == 403


def test_native_flow_uses_body_tokens_and_no_cookies(client):
    h = {"X-Client": "native"}
    r = client.post("/api/auth/register", json={"email": "nat@example.com", "password": "native password 1"}, headers=h)
    assert r.status_code == 201
    body = r.json()
    assert body["access_token"] and body["refresh_token"] and not r.headers.get_list("set-cookie")
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    # bearer requests need no CSRF token
    ins = client.post("/api/inspections", json={"url": "https://www.youtube.com/watch?v=abc"}, headers={"Authorization": f"Bearer {body['access_token']}"})
    assert ins.status_code == 202
    rr = client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]}, headers=h)
    assert rr.status_code == 200 and rr.json()["refresh_token"] != body["refresh_token"]
    out = client.post("/api/auth/logout", json={"refresh_token": rr.json()["refresh_token"]}, headers=h)
    assert out.status_code == 204
    assert client.post("/api/auth/refresh", json={"refresh_token": rr.json()["refresh_token"]}, headers=h).status_code == 401


def test_login_rate_limit(client):
    Session(client, "lim@example.com")
    codes = [client.post("/api/auth/login", json={"email": "lim@example.com", "password": "wrong-wrong-wrong"}).status_code for _ in range(12)]
    assert codes[:8] == [401] * 8
    assert 429 in codes[8:]
    r = client.post("/api/auth/login", json={"email": "lim@example.com", "password": "wrong-wrong-wrong"})
    assert r.status_code == 429 and "retry-after" in r.headers


def test_registration_modes(client, settings):
    object.__setattr__(settings, "registration_mode", "closed")
    try:
        assert client.post("/api/auth/register", json={"email": "a@b.co", "password": "long enough password"}).status_code == 403
        object.__setattr__(settings, "registration_mode", "invite")
        object.__setattr__(settings, "registration_code", "sesame")
        assert client.post("/api/auth/register", json={"email": "a@b.co", "password": "long enough password", "invite_code": "no"}).status_code == 403
        assert client.post("/api/auth/register", json={"email": "a@b.co", "password": "long enough password", "invite_code": "sesame"}).status_code == 201
    finally:
        object.__setattr__(settings, "registration_mode", "open")
        object.__setattr__(settings, "registration_code", "")
