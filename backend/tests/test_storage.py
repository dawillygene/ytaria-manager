import os
import uuid

import pytest

from app.services import storage


def test_resolve_inside_rejects_traversal_and_absolute(tmp_path):
    root = tmp_path / "media"
    (root / "jobs").mkdir(parents=True)
    (root / "jobs" / "f.mp4").write_bytes(b"x")
    assert storage.resolve_inside(root, "jobs/f.mp4").name == "f.mp4"
    for bad in ("../etc/passwd", "jobs/../../etc/passwd", "/etc/passwd", "", "jobs//f.mp4/../..", "a\x00b", "\\windows"):
        with pytest.raises(storage.UnsafePath):
            storage.resolve_inside(root, bad)


def test_symlink_components_and_final_symlink_are_refused(tmp_path, settings):
    root = storage.media_root(settings)
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    d = root / "jobs" / str(uuid.uuid4()) / "final"
    d.mkdir(parents=True)
    link = d / "media.mp4"
    link.symlink_to(outside)
    rel = link.relative_to(root).as_posix()
    with pytest.raises(storage.UnsafePath):
        storage.open_stored_file(settings, rel)
    # symlinked directory component
    real = tmp_path / "realdir"
    real.mkdir()
    (real / "media.mp4").write_bytes(b"data")
    linkdir = root / "jobs" / "linkdir"
    linkdir.symlink_to(real)
    with pytest.raises(storage.UnsafePath):
        storage.open_stored_file(settings, "jobs/linkdir/media.mp4")


def test_open_stored_file_regular(settings):
    root = storage.media_root(settings)
    p = root / "jobs" / "x" / "final"
    p.mkdir(parents=True)
    (p / "media.mp4").write_bytes(b"12345")
    fd, st = storage.open_stored_file(settings, "jobs/x/final/media.mp4")
    try:
        assert st.st_size == 5 and os.read(fd, 10) == b"12345"
    finally:
        os.close(fd)


def test_remove_tree_refuses_outside_root_and_does_not_follow_symlinks(tmp_path, settings):
    root = storage.media_root(settings)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    jd = root / "jobs" / str(uuid.uuid4())
    jd.mkdir(parents=True)
    (jd / "link").symlink_to(victim)
    storage.remove_tree(jd, root)
    assert not jd.exists() and (victim / "keep.txt").exists()
    with pytest.raises(storage.UnsafePath):
        storage.remove_tree(tmp_path / "victim", root)


def test_find_output_file_skips_partials_symlinks_and_stream_files(tmp_path):
    w = tmp_path
    (w / "abc.f137.mp4").write_bytes(b"v" * 500)
    (w / "abc.f140.m4a").write_bytes(b"a" * 300)
    (w / "abc.mp4.part").write_bytes(b"p" * 900)
    (w / "abc.mp4.aria2").write_bytes(b"c" * 900)
    (w / "evil.mp4").symlink_to("/etc/hostname")
    assert storage.find_output_file(w) is None
    (w / "abc.mp4").write_bytes(b"m" * 700)
    found = storage.find_output_file(w)
    assert found and found.path.name == "abc.mp4" and found.size == 700


def test_filenames_are_sanitised():
    assert storage.sanitize_filename('../../etc/pass"wd\r\n', "mp4") == "etc pass wd.mp4"
    assert storage.sanitize_filename("", "mp3") == "download.mp3"
    assert storage.sanitize_filename("a" * 500, "mp4").endswith(".mp4") and len(storage.sanitize_filename("a" * 500, "mp4")) <= 125
    cd = storage.content_disposition('Café "quoted"; x.mp4')
    assert cd.startswith("attachment;") and "\r" not in cd and "\n" not in cd
    assert 'filename="Cafe' in cd or 'filename="Caf' in cd
