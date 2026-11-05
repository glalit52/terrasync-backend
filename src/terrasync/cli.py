"""`terrasync` -- run the service.

    terrasync serve            the API and web application (uvicorn)
    terrasync worker           the monitoring schedule, forever
    terrasync worker --once    one monitoring cycle, then exit (cron, CI)
    terrasync bootstrap        create the first administrator from env vars
    terrasync seed-demo        enrol the four demo sites for an organisation

Everything is configured by environment variables; see README.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date


def _settings():
    from .config import Settings
    return Settings.from_env()


def cmd_serve(args) -> int:
    import uvicorn

    from .app import create_app
    settings = _settings()
    #: One process. SQLite takes one writer, the auth throttle is in memory,
    #: and the engine's pipeline is CPU-bound Python: more processes would
    #: buy contention, not throughput. Scale by moving to PostgreSQL first.
    uvicorn.run(create_app(settings), host=args.host, port=args.port,
                proxy_headers=True,
                forwarded_allow_ips=settings.forwarded_allow_ips,
                access_log=False, log_config=None)
    return 0


def cmd_worker(args) -> int:
    from . import worker
    from .provisioning import prepare_database
    settings = _settings()
    prepare_database(settings.db_path)
    if args.once:
        today = date.fromisoformat(args.today) if args.today else None
        summary = worker.run_cycle(settings, today=today)
        print(json.dumps(summary, indent=2, default=str))
        return 1 if summary["errors"] else 0
    worker.run_forever(settings)
    return 0


def cmd_bootstrap(args) -> int:
    from .provisioning import ensure_admin, prepare_database
    settings = _settings()
    prepare_database(settings.db_path)
    if not (settings.admin_email and settings.admin_password):
        print("set TERRASYNC_ADMIN_EMAIL and TERRASYNC_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    org = ensure_admin(settings)
    print(f"created organisation {org}" if org else
          f"{settings.admin_email} already exists; nothing to do")
    return 0


def cmd_seed_demo(args) -> int:
    from .provisioning import org_for_email, prepare_database, seed_demo
    settings = _settings()
    prepare_database(settings.db_path)
    email = args.email or settings.admin_email
    if not email:
        print("pass --email or set TERRASYNC_ADMIN_EMAIL", file=sys.stderr)
        return 2
    org = org_for_email(settings.db_path, email)
    out = seed_demo(settings.db_path, org, date.fromisoformat(args.since),
                    date.fromisoformat(args.to), sites_only=args.sites_only,
                    only=tuple(args.site))
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="terrasync")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("serve", help="run the API and web application")
    q.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    q.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    q.set_defaults(fn=cmd_serve)

    q = sub.add_parser("worker", help="run the monitoring schedule")
    q.add_argument("--once", action="store_true", help="one cycle, then exit")
    q.add_argument("--today", default="", help="treat this date as today (YYYY-MM-DD)")
    q.set_defaults(fn=cmd_worker)

    q = sub.add_parser("bootstrap", help="create the first administrator")
    q.set_defaults(fn=cmd_bootstrap)

    q = sub.add_parser("seed-demo", help="enrol and monitor the demo estate")
    q.add_argument("--email", default="", help="an account in the target organisation")
    q.add_argument("--from", dest="since", default="2026-03-01")
    q.add_argument("--to", default="2026-04-12")
    q.add_argument("--sites-only", action="store_true",
                   help="enrol the sites without the synthetic backfill, so the "
                        "worker monitors them on the configured provider")
    q.add_argument("--site", action="append", default=[],
                   help="only this site id (repeatable)")
    q.set_defaults(fn=cmd_seed_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("TERRASYNC_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
