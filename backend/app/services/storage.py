"""Private storage: server-generated paths, symlink-safe access, quotas and filenames."""

from __future__ import annotations

import os
import re
import shutil
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..config import Settings

ALLOWED_EXTENSIONS: dict[str, str] = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "ogv": "video/ogg",
    "m4v": "video/x-m4v",
    "avi": "video/x-msvideo",
    "mpg": "video/mpeg",
    "mpeg": "video/mpeg",
    "flv": "video/x-flv",
}
_TEMP_SUFFIXES = (".part", ".ytdl", ".aria2", ".temp", ".tmp", ".frag", ".fragment")


class UnsafePath(Exception):
    pass


@dataclass(frozen=True)
class FoundFile:
    path: Path
    size: int
    ext: str


def media_root(settings: Settings) -> Path:
    return settings.media_root.resolve()


def job_dir(settings: Settings, job_id: uuid.UUID) -> Path:
    return media_root(settings) / "jobs" / str(job_id)


def work_dir(settings: Settings, job_id: uuid.UUID) -> Path:
    return job_dir(settings, job_id) / "work"


def final_dir(settings: Settings, job_id: uuid.UUID) -> Path:
    return job_dir(settings, job_id) / "final"


def ensure_job_dirs(settings: Settings, job_id: uuid.UUID) -> Path:
    root = media_root(settings)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = work_dir(settings, job_id)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    return work


def _check_no_symlinks(root: Path, target: Path) -> None:
    """Every component from ``root`` down to ``target`` must exist as a non-symlink."""
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise UnsafePath("missing") from None
        if stat.S_ISLNK(st.st_mode):
            raise UnsafePath("symlink in path")


def resolve_inside(root: Path, relpath: str) -> Path:
    """Map a stored relative path to an absolute one, refusing traversal and symlinks."""
    if not relpath or "\x00" in relpath or relpath.startswith(("/", "\\")):
        raise UnsafePath("bad path")
    rel = Path(relpath)
    if rel.is_absolute() or any(p in ("..", "") for p in rel.parts):
        raise UnsafePath("traversal")
    root = root.resolve()
    target = root / rel
    if root not in target.resolve(strict=False).parents and target.resolve(strict=False) != root:
        raise UnsafePath("escapes root")
    _check_no_symlinks(root, target)
    return target


def open_stored_file(settings: Settings, relpath: str) -> tuple[int, os.stat_result]:
    """Open a stored file read-only without following symlinks. Caller owns the fd."""
    target = resolve_inside(media_root(settings), relpath)
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePath("not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                total += dir_size(Path(entry.path))
            elif stat.S_ISREG(st.st_mode):
                total += st.st_size
    except FileNotFoundError:
        return 0
    return total


def remove_tree(path: Path, root: Path) -> None:
    """Delete ``path`` only if it lives under ``root`` and is not itself a symlink."""
    root = root.resolve()
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    try:
        resolved = path.resolve()
    except OSError:
        return
    if root not in resolved.parents:
        raise UnsafePath("refusing to delete outside media root")
    shutil.rmtree(resolved, ignore_errors=True)  # rmtree never follows symlinked subdirectories


def remove_job_files(settings: Settings, job_id: uuid.UUID) -> None:
    d = job_dir(settings, job_id)
    if d.exists() or d.is_symlink():
        remove_tree(d, media_root(settings))


def remove_work_dir(settings: Settings, job_id: uuid.UUID) -> None:
    d = work_dir(settings, job_id)
    if d.exists() or d.is_symlink():
        remove_tree(d, media_root(settings))


def free_disk_bytes(settings: Settings) -> int:
    root = settings.media_root
    root.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(root).free


def find_output_file(work: Path) -> FoundFile | None:
    """Pick the finished media file in a yt-dlp work directory (largest allowed regular file)."""
    best: FoundFile | None = None
    try:
        entries = list(os.scandir(work))
    except FileNotFoundError:
        return None
    for entry in entries:
        name = entry.name.lower()
        if name.endswith(_TEMP_SUFFIXES) or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        # Intermediate per-stream files look like "title.f137.mp4"; the merged file has no .fNNN tag.
        if ext not in ALLOWED_EXTENSIONS or re.search(r"\.f\d+\.[a-z0-9]+$", name):
            continue
        size = entry.stat(follow_symlinks=False).st_size
        if size > 0 and (best is None or size > best.size):
            best = FoundFile(Path(entry.path), size, ext)
    return best


def has_unsupported_output(work: Path) -> bool:
    """True if the work dir holds a finished-looking regular file whose type we do not serve."""
    try:
        for entry in os.scandir(work):
            name = entry.name.lower()
            if not name.endswith(_TEMP_SUFFIXES) and entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_size > 0:
                return True
    except FileNotFoundError:
        pass
    return False


def finalize_file(settings: Settings, job_id: uuid.UUID, found: FoundFile) -> str:
    """Move the finished file to ``final/media.<ext>``; return its media-root-relative path."""
    dest_dir = final_dir(settings, job_id)
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = dest_dir / f"media.{found.ext}"
    os.replace(found.path, dest)
    os.chmod(dest, 0o600)
    return dest.relative_to(media_root(settings)).as_posix()


_BAD_NAME = re.compile(r'[\x00-\x1f\x7f"\\/:*?<>|]+')


def sanitize_filename(title: str | None, ext: str, fallback: str = "download") -> str:
    """Human-friendly download name derived from an untrusted title."""
    text = unicodedata.normalize("NFC", title or "")
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = _BAD_NAME.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[:120].strip(" .") or fallback
    return f"{text}.{ext}"


def content_disposition(filename: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", ascii_name).strip() or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def mime_for_ext(ext: str) -> str:
    return ALLOWED_EXTENSIONS.get(ext.lower(), "application/octet-stream")
