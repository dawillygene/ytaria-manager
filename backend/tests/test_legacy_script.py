"""The legacy single-user script must no longer contain the unsafe behaviours found in the baseline review."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ytaria_legacy", ROOT / "ytaria.py")
legacy = importlib.util.module_from_spec(spec)
sys.modules["ytaria_legacy"] = legacy
spec.loader.exec_module(legacy)


@pytest.mark.parametrize("bad", ["--exec=touch /tmp/x", "-o /etc/passwd", "file:///etc/passwd", "ftp://x/y", "", "https://u:p@x.com/", "https://x.com/a b", "javascript:alert(1)"])
def test_validate_url_rejects(bad):
    with pytest.raises(ValueError):
        legacy.validate_url(bad)


def test_command_uses_double_dash_ignore_config_and_finite_retries():
    cmd = legacy.build_yt_dlp_command("https://example.com/v", Path("/out"))
    assert cmd[-2:] == ["--", "https://example.com/v"] and "--ignore-config" in cmd
    joined = " ".join(cmd)
    assert "infinite" not in joined and "cookies" not in joined
    assert not hasattr(legacy, "SUPPORTED_COOKIE_BROWSERS") and not hasattr(legacy, "COOKIES_FROM_BROWSER")


def test_rows_never_expose_command_line(tmp_path):
    store = legacy.JobStore(tmp_path / "j.db")
    jid = store.add_job("https://example.com/v", tmp_path, "yt-dlp SECRET-FLAGS")
    public = legacy.dict_from_row(store.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    assert "command" not in public and "cookies_browser" not in public and "SECRET" not in json.dumps(public)


def test_web_api_ignores_client_output_dir_and_rejects_bad_urls(tmp_path, monkeypatch):
    import http.client
    import threading

    monkeypatch.setattr(legacy, "DEFAULT_OUTPUT_DIR", tmp_path / "safe")
    app = legacy.App(tmp_path / "j.db")
    server = legacy.AppServer(("127.0.0.1", 0), legacy.APIHandler, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        def post(payload):
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            c.request("POST", "/api/jobs", json.dumps(payload), {"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, r.read()

        status, _ = post({"url": "https://example.com/v", "output_dir": "/etc/cron.d"})
        assert status == 200
        row = app.store.get_job(1)
        assert row["output_dir"] == str(tmp_path / "safe")
        assert post({"url": "--exec=id"})[0] == 400
        assert post({"url": "https://example.com/v", "pad": "x" * 70000})[0] == 400
    finally:
        server.shutdown()
        server.server_close()
