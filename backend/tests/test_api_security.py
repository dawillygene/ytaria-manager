def test_security_headers_and_error_shape(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    for h, v in {"x-content-type-options": "nosniff", "x-frame-options": "DENY", "referrer-policy": "no-referrer"}.items():
        assert r.headers[h] == v
    assert r.headers["cache-control"] == "no-store" and r.headers["x-request-id"]
    e = client.get("/api/jobs")
    assert e.status_code == 401 and set(e.json()["error"]) == {"code", "message"}
    assert client.get("/api/nope").json()["error"]["code"] == "http_404"


def test_request_body_limit(client, settings):
    big = "x" * (settings.max_body_bytes + 10)
    r = client.post("/api/auth/login", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "body_too_large"
    # chunked body without Content-Length
    r = client.post("/api/auth/login", content=iter([b"x" * 40000, b"x" * 40000]), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_cors_only_for_configured_origins(client, settings, monkeypatch):
    # the middleware captured the origins at app creation; test the default (none configured)
    r = client.options("/api/auth/login", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_the_capacitor_origin_when_configured(monkeypatch):
    monkeypatch.setenv("YTARIA_CORS_ORIGINS", "https://localhost,https://app.example.com")
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    import app.main as main

    monkeypatch.setattr(main, "get_settings", lambda: Settings())
    with TestClient(create_app()) as c:
        ok = c.options("/api/auth/login", headers={"Origin": "https://localhost", "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "authorization,x-client,idempotency-key"})
        assert ok.headers["access-control-allow-origin"] == "https://localhost" and ok.headers["access-control-allow-credentials"] == "true"
        bad = c.options("/api/auth/login", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in bad.headers


def test_unknown_fields_and_bad_types_are_rejected(alice):
    assert alice.post("/api/inspections", {"url": "https://www.youtube.com/watch?v=1", "cookies_from_browser": "firefox"}).status_code == 422
    assert alice.post("/api/inspections", {"url": 123}).status_code == 422
    assert alice.post("/api/jobs", {"inspection_id": "not-a-uuid", "selection": "best"}).status_code == 422
    assert alice.get("/api/jobs/not-a-uuid").status_code == 422


def test_inspection_rejects_ssrf_targets_before_anything_is_queued(alice, dispatcher):
    for url in ("http://169.254.169.254/latest/meta-data", "http://localhost:8000/", "file:///etc/passwd", "https://user:pw@www.youtube.com/x",
                "https://www.youtube.com:8443/x", "http://10.0.0.1/", "http://[::1]/", "https://evil.example/x"):
        r = alice.post("/api/inspections", {"url": url})
        assert r.status_code == 422, (url, r.text)
    assert dispatcher.inspections == []


def test_inspection_reuses_live_result_and_limits_pending(alice, dispatcher, settings, monkeypatch):
    a = alice.post("/api/inspections", {"url": "https://www.youtube.com/watch?v=one"})
    b = alice.post("/api/inspections", {"url": "https://www.youtube.com/watch?v=one#t=5"})
    assert a.status_code == 202 and b.status_code == 200 and a.json()["id"] == b.json()["id"]
    assert len(dispatcher.inspections) == 1
    for i in range(2, 4):
        assert alice.post("/api/inspections", {"url": f"https://www.youtube.com/watch?v=v{i}"}).status_code == 202
    r = alice.post("/api/inspections", {"url": "https://www.youtube.com/watch?v=v99"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_inspections"


def test_rate_limits_on_authenticated_actions(alice, settings, monkeypatch):
    monkeypatch.setattr(settings, "rl_inspect_user", "2/60")
    codes = [alice.post("/api/inspections", {"url": f"https://www.youtube.com/watch?v=r{i}"}).status_code for i in range(4)]
    assert codes[:2] == [202, 202] and codes[2] == 429


def test_ready_endpoint_reports_dependencies(client):
    r = client.get("/api/ready")
    assert r.json()["database"] == "ok" and r.json()["redis"] == "ok"


def test_public_config_exposes_no_secrets(client):
    body = client.get("/api/config").json()
    assert set(body) == {"app", "registration", "password_min_length"}


def test_ready_scope_lists_only_downloadable_files(alice, new_job, settings):
    from tests.helpers import complete_with_file

    done = new_job(alice)
    complete_with_file(settings, done["id"])
    new_job(alice)
    ready = alice.get("/api/jobs?scope=ready").json()
    assert [j["id"] for j in ready["items"]] == [done["id"]] and ready["total"] == 1


def test_rate_limiter_fails_closed_when_backend_is_down(client, monkeypatch):
    from app import ratelimit

    class Broken:
        def incr(self, key, window):
            raise ConnectionError("redis down")

    monkeypatch.setattr(ratelimit, "get_backend", lambda: Broken())
    r = client.post("/api/auth/login", json={"email": "a@b.co", "password": "whatever-long"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "rate_limiter_unavailable"
