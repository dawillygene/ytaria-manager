"""Stand-in for yt-dlp used by executor tests: behaviour chosen by argv[1]."""
import os
import subprocess
import sys
import time
from pathlib import Path

mode, work = sys.argv[1], Path(sys.argv[2])
pidfile = sys.argv[3] or None
delay = float(sys.argv[4])
work.mkdir(parents=True, exist_ok=True)


def progress(n):
    print(f"[#abc123 {n}MiB/100MiB({n}%) CN:4 DL:2.0MiB ETA:10s]", flush=True)


if mode == "ok":
    print("[download] Destination: vid.f137.mp4", flush=True)
    for n in (10, 50, 90):
        progress(n)
    (work / "vid.mp4").write_bytes(b"\x00" * 4096)
    print('[Merger] Merging formats into "vid.mp4"', flush=True)
elif mode == "slow":  # long-running with a grandchild, like yt-dlp -> aria2c
    child = subprocess.Popen(["sleep", "120"])
    if pidfile:
        Path(pidfile).write_text(str(child.pid))
    (work / "vid.mp4.part").write_bytes(b"p" * 1000)
    progress(5)
    time.sleep(120)
elif mode == "late":  # finishes successfully after a delay (cancel/completion race)
    progress(50)
    time.sleep(delay)
    (work / "vid.mp4").write_bytes(b"\x01" * 2048)
elif mode == "resume":  # succeeds only if partial data from the previous attempt is still there
    if not (work / "vid.mp4.part").exists():
        print("ERROR: partial data missing", flush=True)
        sys.exit(1)
    (work / "vid.mp4").write_bytes(b"\x02" * 2048)
    (work / "vid.mp4.part").unlink()
elif mode == "net":
    print("ERROR: unable to download video data: HTTP Error 503: Service Unavailable", flush=True)
    sys.exit(1)
elif mode == "private":
    print("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", flush=True)
    sys.exit(1)
elif mode == "weird":
    (work / "vid.xyz").write_bytes(b"1" * 100)
elif mode == "empty":
    pass
elif mode == "big":
    (work / "vid.mp4.part").write_bytes(b"\x00" * (3 * 1024 * 1024))
    time.sleep(120)
