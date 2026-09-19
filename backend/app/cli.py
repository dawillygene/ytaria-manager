"""Operator CLI: ``python -m app.cli <command>``."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import func, select

from .config import get_settings
from .db import session_scope


def cmd_create_user(args: argparse.Namespace) -> int:
    from .models import User
    from .security import hash_password

    password = args.password or getpass.getpass("Password: ")
    settings = get_settings()
    if len(password) < settings.password_min_length:
        print(f"password must be at least {settings.password_min_length} characters", file=sys.stderr)
        return 2
    with session_scope() as s:
        email = args.email.strip().lower()
        if s.scalar(select(User.id).where(User.email == email)):
            print("user already exists", file=sys.stderr)
            return 1
        s.add(User(email=email, display_name=args.name or email.split("@")[0], password_hash=hash_password(password)))
    print(f"created {email}")
    return 0


def cmd_set_quota(args: argparse.Namespace) -> int:
    from .models import User

    with session_scope() as s:
        user = s.execute(select(User).where(User.email == args.email.strip().lower())).scalar_one_or_none()
        if user is None:
            print("no such user", file=sys.stderr)
            return 1
        user.quota_bytes = None if args.gib is None else int(args.gib * 1024**3)
    return 0


def cmd_reconcile(_: argparse.Namespace) -> int:
    from .worker.tasks import reconcile_task

    print(reconcile_task())
    return 0


def cmd_cleanup(_: argparse.Namespace) -> int:
    from .worker.tasks import cleanup_task

    print(cleanup_task())
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    from .models import Job, User
    from .services import storage

    settings = get_settings()
    with session_scope() as s:
        by_status = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        users = s.scalar(select(func.count()).select_from(User))
        used = s.scalar(select(func.coalesce(func.sum(Job.disk_bytes), 0)))
    print({"users": users, "jobs_by_status": by_status, "disk_bytes_tracked": int(used or 0), "disk_free_bytes": storage.free_disk_bytes(settings)})
    return 0


def cmd_import_legacy(args: argparse.Namespace) -> int:
    from .services.legacy_import import import_legacy

    settings = get_settings()
    files_root = Path(args.files_root).expanduser()
    try:
        with session_scope() as s:
            report = import_legacy(s, settings, sqlite_path=Path(args.sqlite).expanduser(), owner_email=args.owner,
                                   files_root=files_root, dry_run=args.dry_run)
            if args.dry_run:
                s.rollback()
    except (FileNotFoundError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(("DRY RUN - nothing was written\n" if args.dry_run else "") + report.summary())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="ytaria-manager operator commands")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user", help="Create an account (useful when registration is closed)")
    c.add_argument("email")
    c.add_argument("--password", help="omit to be prompted")
    c.add_argument("--name")
    c.set_defaults(func=cmd_create_user)
    q = sub.add_parser("set-quota", help="Override a user's storage quota (GiB); omit --gib to reset")
    q.add_argument("email")
    q.add_argument("--gib", type=float)
    q.set_defaults(func=cmd_set_quota)
    sub.add_parser("reconcile", help="Run lease recovery + redispatch once").set_defaults(func=cmd_reconcile)
    sub.add_parser("cleanup", help="Run retention cleanup once").set_defaults(func=cmd_cleanup)
    sub.add_parser("stats", help="Print queue and disk statistics").set_defaults(func=cmd_stats)
    i = sub.add_parser("import-legacy", help="Import finished jobs from the legacy ytaria.py SQLite database")
    i.add_argument("--sqlite", required=True, help="path to the legacy jobs.sqlite3")
    i.add_argument("--owner", required=True, help="email of the existing user who will own every imported job")
    i.add_argument("--files-root", default="~/Downloads/ytaria-downloads", help="only files below this directory are imported")
    i.add_argument("--dry-run", action="store_true", help="report what would happen; write nothing")
    i.set_defaults(func=cmd_import_legacy)
    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
