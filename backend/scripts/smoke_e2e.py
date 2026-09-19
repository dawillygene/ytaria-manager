#!/usr/bin/env python3
"""End-to-end smoke test against a *running* deployment (API + worker [+ scheduler]).

Uses only a media URL you are authorised to download (default: a public-domain film on archive.org).
Exercises: register two users, inspect, queue, progress, pause/resume, cancel, cross-user isolation,
ticketed + ranged file delivery, device-saved report. Exits non-zero on the first failed check.

    python scripts/smoke_e2e.py --base-url http://127.0.0.1:8000 --url https://archive.org/details/Popeye_forPresident
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
import uuid

import httpx


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


class Client:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=60)
        self.csrf = ""

    def register(self, email: str, password: str) -> None:
        r = self.http.post("/api/auth/register", json={"email": email, "password": password})
        check(r.status_code == 201, f"register {email}")
        self.csrf = r.json()["csrf_token"]

    def req(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"X-CSRF-Token": self.csrf, **kw.pop("headers", {})}
        return self.http.request(method, path, headers=headers, **kw)

    def wait(self, path: str, done, timeout: float = 300, label: str = "") -> dict:
        end = time.time() + timeout
        last = {}
        while time.time() < end:
            last = self.req("GET", path).json()
            if done(last):
                return last
            time.sleep(1)
        print(last)
        check(False, f"timeout waiting for {label or path}")
        return last


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--url", default="https://archive.org/details/Popeye_forPresident")
    ap.add_argument("--selection", default="best")
    args = ap.parse_args()
    tag = uuid.uuid4().hex[:8]
    alice, bob = Client(args.base_url), Client(args.base_url)
    alice.register(f"smoke-a-{tag}@example.com", "smoke test password 1")
    bob.register(f"smoke-b-{tag}@example.com", "smoke test password 2")

    insp = alice.req("POST", "/api/inspections", json={"url": args.url}).json()
    insp = alice.wait(f"/api/inspections/{insp['id']}", lambda d: d["status"] in ("succeeded", "failed"), 90, "inspection")
    check(insp["status"] == "succeeded", f"inspection succeeded: {insp.get('title')!r} options={[o['id'] for o in insp['options']]}")
    check(bob.req("GET", f"/api/inspections/{insp['id']}").status_code == 404, "other user cannot read the inspection")
    sel = args.selection if any(o["id"] == args.selection for o in insp["options"]) else insp["options"][0]["id"]

    # --- pause / resume --------------------------------------------------------------------------
    key = uuid.uuid4().hex
    job = alice.req("POST", "/api/jobs", json={"inspection_id": insp["id"], "selection": sel}, headers={"Idempotency-Key": key}).json()
    again = alice.req("POST", "/api/jobs", json={"inspection_id": insp["id"], "selection": sel}, headers={"Idempotency-Key": key})
    check(again.status_code == 200 and again.json()["id"] == job["id"], "duplicate submission returns the same job")
    jid = job["id"]
    j = alice.wait(f"/api/jobs/{jid}", lambda d: d["status"] == "running" and d["progress_percent"] > 0, 120, "download to start")
    check(True, f"running: {j['progress_percent']}% stage={j['stage']}")
    check(alice.req("POST", f"/api/jobs/{jid}/pause").json()["status"] in ("pausing", "paused"), "pause requested")
    j = alice.wait(f"/api/jobs/{jid}", lambda d: d["status"] == "paused", 60, "pause")
    check(True, f"paused at {j['progress_percent']}% (partial data kept)")
    check(alice.req("POST", f"/api/jobs/{jid}/resume").json()["status"] in ("queued", "running"), "resume requested")
    j = alice.wait(f"/api/jobs/{jid}", lambda d: d["status"] in ("completed", "failed"), 600, "completion")
    check(j["status"] == "completed", f"completed: {j['file']}")
    check(j["delivery"]["state"] == "ready", "ready on server (not yet on device)")

    # --- isolation -------------------------------------------------------------------------------
    for m, p in (("GET", f"/api/jobs/{jid}"), ("GET", f"/api/jobs/{jid}/file"), ("POST", f"/api/jobs/{jid}/cancel"), ("POST", f"/api/jobs/{jid}/download-ticket")):
        check(bob.req(m, p).status_code == 404, f"other user gets 404 for {m} {p.split(jid)[1] or '/'}")

    # --- delivery --------------------------------------------------------------------------------
    size = j["file"]["size"]
    ticket = alice.req("POST", f"/api/jobs/{jid}/download-ticket").json()
    anon = httpx.Client(base_url=args.base_url, timeout=120)
    part = anon.get(ticket["url"], headers={"Range": "bytes=0-1023"})
    check(part.status_code == 206 and len(part.content) == 1024, "ranged transfer via ticket (206)")
    h = hashlib.sha256()
    got = 0
    with anon.stream("GET", ticket["url"]) as resp:
        check(resp.status_code == 200 and resp.headers["content-disposition"].startswith("attachment"), "full transfer via ticket")
        for chunk in resp.iter_bytes(1 << 20):
            h.update(chunk)
            got += len(chunk)
    check(got == size, f"transferred {got} of {size} bytes, sha256={h.hexdigest()[:16]}…")
    check(alice.req("GET", f"/api/jobs/{jid}").json()["delivery"]["state"] == "sent", "server marks transfer sent (not saved)")
    check(alice.req("POST", f"/api/jobs/{jid}/transfer", json={"state": "saved"}).status_code == 204, "device reports saved")
    check(alice.req("GET", f"/api/jobs/{jid}").json()["delivery"]["state"] == "saved", "delivery state is saved")
    usage = alice.req("GET", "/api/usage").json()
    check(usage["used_bytes"] >= size, f"usage accounted: {usage['used_bytes']} bytes")

    # --- cancel ----------------------------------------------------------------------------------
    job2 = alice.req("POST", "/api/jobs", json={"inspection_id": insp["id"], "selection": sel}).json()
    alice.wait(f"/api/jobs/{job2['id']}", lambda d: d["status"] == "running", 120, "second download to start")
    check(alice.req("POST", f"/api/jobs/{job2['id']}/cancel").json()["status"] in ("canceling", "canceled"), "cancel requested")
    j2 = alice.wait(f"/api/jobs/{job2['id']}", lambda d: d["status"] == "canceled", 60, "cancel")
    check(j2["status"] == "canceled", "canceled (never completes)")
    time.sleep(3)
    check(alice.req("GET", f"/api/jobs/{job2['id']}").json()["status"] == "canceled", "still canceled after the process is gone")
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
