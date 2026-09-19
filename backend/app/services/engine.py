"""yt-dlp / aria2c / ffmpeg integration: safe command construction and output parsing.

Design rules:

* Commands are argument arrays, never shell strings.
* The URL is always preceded by ``--`` so it can never be parsed as an option.
* Clients choose a *selection id* from a fixed policy table; they never supply yt-dlp arguments.
* yt-dlp runs with ``--ignore-config`` and a scrubbed environment; the generic extractor is disabled
  so only site-specific extractors (which we allow-list) fetch pages.
* Network access goes through the egress proxy when one is configured (yt-dlp ``--proxy`` is
  forwarded to aria2c as ``--all-proxy`` and to ffmpeg as ``-http_proxy``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings

# ---------------------------------------------------------------------------------------------
# Selection policy
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    id: str
    label: str
    kind: str  # video | audio
    height: int | None
    format: str
    extra_args: tuple[str, ...] = ()


HEIGHT_LADDER = (144, 240, 360, 480, 720, 1080, 1440, 2160, 4320)


def _video(height: int) -> Selection:
    return Selection(
        id=f"h{height}",
        label=f"{height}p",
        kind="video",
        height=height,
        format=f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b",
        extra_args=("--merge-output-format", "mp4"),
    )


SELECTIONS: dict[str, Selection] = {
    "best": Selection("best", "Best available", "video", None, "bv*+ba/b", ("--merge-output-format", "mp4")),
    **{f"h{h}": _video(h) for h in HEIGHT_LADDER},
    "audio_m4a": Selection("audio_m4a", "Audio (M4A)", "audio", None, "ba[ext=m4a]/ba/b", ("-x", "--audio-format", "m4a")),
    "audio_mp3": Selection("audio_mp3", "Audio (MP3)", "audio", None, "ba/b", ("-x", "--audio-format", "mp3")),
}

_SELECTION_ID = re.compile(r"^[a-z0-9_]{2,24}$")


def get_selection(selection_id: str) -> Selection | None:
    if not _SELECTION_ID.match(selection_id or ""):
        return None
    return SELECTIONS.get(selection_id)


# ---------------------------------------------------------------------------------------------
# Environment and commands
# ---------------------------------------------------------------------------------------------

_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")


def safe_env(settings: Settings, cache_home: Path) -> dict[str, str]:
    """Environment for subprocesses: no application secrets, no inherited proxy/config variables."""
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(cache_home)
    env["XDG_CACHE_HOME"] = str(cache_home / "cache")
    env["XDG_CONFIG_HOME"] = str(cache_home / "config")  # empty: nothing to inherit
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if settings.egress_proxy_url:
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
            env[key] = settings.egress_proxy_url
        env["NO_PROXY"] = env["no_proxy"] = ""
    return env


def _common_args(settings: Settings) -> list[str]:
    args = [
        settings.yt_dlp_bin,
        "--ignore-config",
        "--no-plugin-dirs",
        "--use-extractors", "default,-generic",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout", "30",
        "--retries", "5",
        "--fragment-retries", "5",
        "--extractor-retries", "3",
        "--file-access-retries", "3",
        "--no-exec",
    ]
    if settings.egress_proxy_url:
        args += ["--proxy", settings.egress_proxy_url]
    return args


def build_inspect_command(settings: Settings, url: str, cache_dir: Path) -> list[str]:
    return [
        *_common_args(settings),
        "--cache-dir", str(cache_dir),
        "--dump-single-json",
        "--skip-download",
        "--", url,
    ]


def build_download_command(settings: Settings, url: str, selection: Selection, work: Path, cache_dir: Path) -> list[str]:
    cmd = [
        *_common_args(settings),
        "--cache-dir", str(cache_dir),
        "-f", selection.format,
        *selection.extra_args,
        "--newline",
        "--restrict-filenames",
        "--continue",
        "--max-filesize", str(settings.max_file_bytes),
        "--ffmpeg-location", shutil.which(settings.ffmpeg_bin) or settings.ffmpeg_bin,
        "--downloader", "aria2c",
        # Bounded, finite retries (the legacy code used "infinite").
        "--downloader-args",
        "aria2c:--max-tries=5 --retry-wait=3 --timeout=60 --connect-timeout=30 --console-log-level=warn"
        " --summary-interval=1 --file-allocation=none --check-certificate=true",
        "-P", str(work),
        "-o", "%(id)s.%(ext)s",
        "--", url,
    ]
    return cmd


# ---------------------------------------------------------------------------------------------
# Progress parsing
# ---------------------------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_SIZE = r"(?P<{n}>[0-9.]+)\s*(?P<{n}u>[KMGT]?)i?B"
# aria2c: "[#e771c3 174MiB/590MiB(29%) CN:16 DL:2.3MiB ETA:2m57s]"
ARIA_RE = re.compile(
    r"\[#\w+\s+" + _SIZE.format(n="done") + r"/" + _SIZE.format(n="total") + r"\((?P<pct>\d+)%\)"
    r"(?:.*?DL:" + _SIZE.format(n="dl") + r")?(?:.*?ETA:(?P<eta>[0-9dhms]+))?"
)
# yt-dlp: "[download]  45.2% of  123.45MiB at  2.3MiB/s ETA 00:12"  (also "~123MiB")
YTDLP_RE = re.compile(
    r"\[download\]\s+(?P<pct>[0-9.]+)%\s+of\s+~?\s*" + _SIZE.format(n="total")
    + r"(?:\s+at\s+" + _SIZE.format(n="dl") + r"/s)?(?:\s+ETA\s+(?P<eta>[0-9:]+))?"
)
DEST_RE = re.compile(r"\[download\]\s+Destination:\s+(?P<name>.+)$")
MERGE_RE = re.compile(r'\[Merger\]|\[ExtractAudio\]|\[VideoRemuxer\]|\[FixupM4a\]|\[Fixup')


@dataclass
class Progress:
    percent: float | None = None
    downloaded: int | None = None
    total: int | None = None
    speed: int | None = None
    eta: int | None = None
    stage: str | None = None
    new_stream: bool = False


def _to_bytes(value: str | None, unit: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value) * _UNITS[(unit or "").upper()])
    except (ValueError, KeyError):
        return None


def parse_eta(text: str | None) -> int | None:
    if not text:
        return None
    if ":" in text:
        secs = 0
        for part in text.split(":"):
            try:
                secs = secs * 60 + int(part)
            except ValueError:
                return None
        return secs
    total, found = 0, False
    for num, unit in re.findall(r"(\d+)([dhms])", text):
        found = True
        total += int(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total if found else None


def parse_line(raw: str) -> Progress | None:
    """Parse one output line from yt-dlp/aria2c. Returns None for uninteresting lines."""
    line = ANSI_RE.sub("", raw).strip()
    if not line:
        return None
    if DEST_RE.search(line):
        return Progress(stage="downloading", new_stream=True)
    if MERGE_RE.search(line):
        return Progress(stage="merging")
    m = ARIA_RE.search(line)
    if m:
        return Progress(
            percent=float(m.group("pct")),
            downloaded=_to_bytes(m.group("done"), m.group("doneu")),
            total=_to_bytes(m.group("total"), m.group("totalu")),
            speed=_to_bytes(m.group("dl"), m.group("dlu")),
            eta=parse_eta(m.group("eta")),
            stage="downloading",
        )
    m = YTDLP_RE.search(line)
    if m:
        pct = min(100.0, float(m.group("pct")))
        total = _to_bytes(m.group("total"), m.group("totalu"))
        return Progress(
            percent=pct,
            downloaded=int(total * pct / 100) if total else None,
            total=total,
            speed=_to_bytes(m.group("dl"), m.group("dlu")),
            eta=parse_eta(m.group("eta")),
            stage="downloading",
        )
    return None


# ---------------------------------------------------------------------------------------------
# Error classification (public messages are fixed strings: raw tool output is never exposed)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    retryable: bool


_FAILURES: list[tuple[re.Pattern[str], Failure]] = [
    (re.compile(r"blocked by policy|X-Ytaria-Egress|destination blocked", re.I),
     Failure("blocked_destination", "The source tried to reach a destination that is not allowed.", False)),
    (re.compile(r"unsupported url|no suitable extractor", re.I),
     Failure("unsupported", "This link is not supported.", False)),
    (re.compile(r"private video|members[- ]only|sign in to confirm|log ?in|login required|requires? (a )?(login|sign)|"
                r"confirm your age|age[- ]restricted|premium", re.I),
     Failure("restricted", "This media requires an account or is restricted, so it can't be downloaded here.", False)),
    (re.compile(r"not available in your country|geo[- ]?restrict|blocked (it )?in your country", re.I),
     Failure("geo_blocked", "This media is not available from the server's region.", False)),
    (re.compile(r"video unavailable|has been removed|no longer available|does not exist|this video is not available|"
                r"http error 404|http error 410|copyright", re.I),
     Failure("unavailable", "This media is unavailable.", False)),
    (re.compile(r"drm", re.I),
     Failure("drm", "This media is protected and cannot be downloaded.", False)),
    (re.compile(r"file is larger than max-filesize|max-filesize", re.I),
     Failure("too_large", "The file is larger than the allowed size.", False)),
    (re.compile(r"requested format is not available|no video formats found", re.I),
     Failure("format_unavailable", "The selected quality is not available for this media.", False)),
    (re.compile(r"http error 429|too many requests", re.I),
     Failure("rate_limited_source", "The source is rate limiting this server. Try again later.", True)),
    (re.compile(r"timed out|timeout|temporary failure|connection (reset|refused|aborted)|network is unreachable|"
                r"http error 5\d\d|remote end closed|incomplete read|unable to download|ssl|bad gateway|"
                r"name or service not known", re.I),
     Failure("network", "A network error interrupted the download.", True)),
]
UNKNOWN_FAILURE = Failure("failed", "The download failed.", True)
_ERROR_LINE = re.compile(r"^(ERROR|error)\b")


def classify_failure(tail: list[str]) -> Failure:
    text = "\n".join(tail)
    errors = "\n".join(line for line in tail if _ERROR_LINE.match(line.lstrip()))
    for haystack in (errors, text):
        if not haystack:
            continue
        for pattern, failure in _FAILURES:
            if pattern.search(haystack):
                return failure
    return UNKNOWN_FAILURE


# ---------------------------------------------------------------------------------------------
# Inspection results
# ---------------------------------------------------------------------------------------------

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_text(value: Any, limit: int = 300) -> str:
    text = _CTRL.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


class InspectionError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass
class InspectionResult:
    title: str
    duration: int | None
    extractor: str
    options: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "duration_seconds": self.duration, "extractor": self.extractor, "options": self.options}


def parse_inspection(raw_json: str, settings: Settings) -> InspectionResult:
    """Turn ``yt-dlp --dump-single-json`` output into a sanitised, policy-constrained result."""
    try:
        info = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise InspectionError("metadata_failed", "Could not read information about this link.", True) from exc
    if not isinstance(info, dict):
        raise InspectionError("metadata_failed", "Could not read information about this link.")
    if info.get("_type") in ("playlist", "multi_video"):
        raise InspectionError("playlist", "Playlists are not supported. Submit a single video link.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise InspectionError("live", "Live streams and upcoming premieres can't be downloaded.")
    duration = info.get("duration")
    duration = int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None
    if duration and duration > settings.max_media_duration_seconds:
        raise InspectionError("too_long", "This media is longer than the allowed duration.")

    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    # yt-dlp reports "none" for a stream that is absent and null when the codec is merely unknown
    # (e.g. archive.org progressive files), so only the literal "none" excludes a format.
    video = [f for f in formats if f.get("vcodec") != "none" and isinstance(f.get("height"), int)]
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    has_audio = bool(audio) or any(f.get("acodec") != "none" for f in video)
    if any(f.get("has_drm") or f.get("drm") for f in formats) and not video:
        raise InspectionError("drm", "This media is protected and cannot be downloaded.")

    def size_of(f: dict[str, Any] | None) -> int:
        if not f:
            return 0
        return int(f.get("filesize") or f.get("filesize_approx") or 0)

    best_audio = max(audio, key=size_of, default=None)
    options: list[dict[str, Any]] = []
    if video:
        max_height = max(f["height"] for f in video)
        min_height = min(f["height"] for f in video)
        rungs = [h for h in HEIGHT_LADDER if min_height <= h <= max_height or h == max_height]
        # Non-ladder maximum (e.g. 800p): still offered through "best".
        options.append({"id": "best", "label": f"Best available ({max_height}p)", "kind": "video",
                        "height": max_height, "ext": "auto", "estimated_bytes": None})
        for h in reversed(rungs):
            candidates = [f for f in video if f["height"] <= h]
            top = max(candidates, key=lambda f: (f["height"], f.get("tbr") or 0))
            est = size_of(top) + (0 if top.get("acodec") != "none" and top.get("acodec") is not None else size_of(best_audio))
            options.append({"id": f"h{h}", "label": f"{h}p", "kind": "video", "height": h, "ext": "mp4",
                            "estimated_bytes": est or None})
    if has_audio:
        est = size_of(best_audio) or None
        options.append({"id": "audio_m4a", "label": "Audio (M4A)", "kind": "audio", "height": None, "ext": "m4a", "estimated_bytes": est})
        options.append({"id": "audio_mp3", "label": "Audio (MP3)", "kind": "audio", "height": None, "ext": "mp3", "estimated_bytes": est})
    if not options:
        raise InspectionError("no_formats", "No downloadable formats were found for this link.")
    # Deduplicate top rung equal to "best" is fine; keep ids unique.
    seen: set[str] = set()
    options = [o for o in options if not (o["id"] in seen or seen.add(o["id"]))]
    return InspectionResult(
        title=clean_text(info.get("title")) or "Untitled",
        duration=duration,
        extractor=clean_text(info.get("extractor_key") or info.get("extractor"), 40),
        options=options,
    )
