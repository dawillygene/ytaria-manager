import json

from app.config import Settings
from app.services import engine


def s(**kw):
    return Settings(_env_file=None, **kw)


def test_download_command_is_an_argv_with_url_after_double_dash():
    st = s(egress_proxy_url="http://proxy:3128")
    sel = engine.get_selection("h720")
    cmd = engine.build_download_command(st, "https://youtube.com/watch?v=x", sel, __import__("pathlib").Path("/w"), __import__("pathlib").Path("/c"))
    assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)
    assert cmd[-2:] == ["--", "https://youtube.com/watch?v=x"]
    assert "--ignore-config" in cmd and "--no-plugin-dirs" in cmd
    assert "--proxy" in cmd and cmd[cmd.index("--proxy") + 1] == "http://proxy:3128"
    assert "default,-generic" in cmd
    joined = " ".join(cmd)
    assert "cookies" not in joined and "infinite" not in joined and "--exec" not in cmd
    assert cmd[cmd.index("--retries") + 1] == "5"


def test_selection_policy_rejects_arbitrary_input():
    assert engine.get_selection("best") and engine.get_selection("audio_mp3")
    for bad in ("", "--exec=x", "bv*+ba", "best; rm -rf /", "H720", "h999", "a" * 100, None):
        assert engine.get_selection(bad) is None


def test_safe_env_has_no_secrets_and_forces_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("YTARIA_SECRET_KEY", "supersecret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker:1")
    env = engine.safe_env(s(egress_proxy_url="http://proxy:3128"), tmp_path)
    assert "YTARIA_SECRET_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HTTP_PROXY"] == env["https_proxy"] == "http://proxy:3128"
    assert env["HOME"] == str(tmp_path)


def test_parse_aria2_and_ytdlp_progress():
    p = engine.parse_line("[#e771c3 174MiB/590MiB(29%) CN:16 DL:2.3MiB ETA:2m57s]")
    assert p and p.percent == 29 and p.downloaded == 174 * 1024**2 and p.total == 590 * 1024**2
    assert p.speed == int(2.3 * 1024**2) and p.eta == 177
    p = engine.parse_line("\x1b[0m[download]  45.2% of  123.45MiB at  2.30MiB/s ETA 01:12")
    assert p and p.percent == 45.2 and p.eta == 72 and p.total == int(123.45 * 1024**2)
    p = engine.parse_line("[download]  10.0% of ~ 1.00GiB at 500.00KiB/s ETA 30:00")
    assert p and p.total == 1024**3
    assert engine.parse_line("[download] Destination: abc.f137.mp4").new_stream
    assert engine.parse_line('[Merger] Merging formats into "abc.mp4"').stage == "merging"
    assert engine.parse_line("random noise") is None and engine.parse_line("") is None


def test_failure_classification():
    c = engine.classify_failure
    assert c(["ERROR: [youtube] x: Sign in to confirm you're not a bot"]).code == "restricted"
    assert c(["ERROR: Unsupported URL: https://x"]).code == "unsupported"
    assert c(["ERROR: Video unavailable"]).code == "unavailable" and not c(["ERROR: Video unavailable"]).retryable
    assert c(["ERROR: unable to download video data: HTTP Error 503"]).retryable
    assert c(["ERROR: HTTP Error 429: Too Many Requests"]).code == "rate_limited_source"
    assert c(["ERROR: Tunnel connection failed: 403 Forbidden destination blocked by policy"]).code == "blocked_destination"
    unknown = c(["something odd"])
    assert unknown.code == "failed"
    # public messages are fixed strings, never raw tool output
    assert "Sign in" not in c(["ERROR: Sign in to confirm you're not a bot"]).message


def _info(**kw):
    base = {"title": "Hello\x00 <script>alert(1)</script>", "duration": 120, "extractor_key": "Youtube", "formats": [
        {"vcodec": "avc1", "acodec": "none", "height": 1080, "filesize": 800},
        {"vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 400},
        {"vcodec": "avc1", "acodec": "none", "height": 360, "filesize": 100},
        {"vcodec": "none", "acodec": "mp4a", "filesize": 50},
    ]}
    base.update(kw)
    return json.dumps(base)


def test_parse_inspection_builds_policy_options():
    r = engine.parse_inspection(_info(), s())
    ids = [o["id"] for o in r.options]
    assert ids[0] == "best" and "h1080" in ids and "h720" in ids and "h360" in ids and "h1440" not in ids
    assert "audio_m4a" in ids and "audio_mp3" in ids
    h720 = next(o for o in r.options if o["id"] == "h720")
    assert h720["estimated_bytes"] == 450
    assert "\x00" not in r.title  # control chars stripped; HTML escaping is the UI's job (rendered as text)


def test_parse_inspection_rejects_playlists_live_and_long():
    import pytest

    for raw, code in [
        (json.dumps({"_type": "playlist", "entries": []}), "playlist"),
        (_info(is_live=True), "live"),
        (_info(duration=10**6), "too_long"),
        ("not json", "metadata_failed"),
        (json.dumps({"title": "x", "formats": []}), "no_formats"),
    ]:
        with pytest.raises(engine.InspectionError) as e:
            engine.parse_inspection(raw, s())
        assert e.value.code == code


def test_unknown_codecs_are_treated_as_playable():
    """archive.org style: vcodec is null, not "none"."""
    raw = json.dumps({"title": "Old film", "duration": 370, "extractor_key": "ArchiveOrg", "formats": [
        {"format_id": "0", "height": 240, "filesize": 26989036, "vcodec": None, "acodec": None},
        {"format_id": "1", "height": 304, "filesize": 26138707, "vcodec": None, "acodec": None}]})
    r = engine.parse_inspection(raw, s())
    ids = [o["id"] for o in r.options]
    assert "best" in ids and "h240" in ids and "audio_mp3" in ids
    assert next(o for o in r.options if o["id"] == "h240")["estimated_bytes"] == 26989036
