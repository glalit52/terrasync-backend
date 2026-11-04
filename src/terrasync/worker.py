"""The scheduled monitoring run: every active area, every settled day, then
deliver what it raised.

The engine's pipeline is idempotent per area-day and must run oldest day
first, because each day's baseline is built from the days before it. So the
worker keeps a ledger of which area-days are settled and advances each area
from where it stopped -- never skipping ahead past a day that failed, until
that day has failed often enough to be recorded as skipped.
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any

from terrashield import pipeline, providers
from terrashield.domain import Role
from terrashield.store import Store

from . import delivery, ledger
from .config import Settings
from .provisioning import ensure_default_rules, prepare_database

log = logging.getLogger("terrasync.worker")

ACTOR = "terrasync-worker"


def holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def days_to_run(conn: sqlite3.Connection, org_id: str, aoi_id: str, today: date,
                settings: Settings) -> list[date]:
    """The days this cycle should process for one area, oldest first."""
    newest = today - timedelta(days=settings.settle_days)
    last = ledger.last_settled_day(conn, org_id, aoi_id)
    first = (last + timedelta(days=1) if last
             else newest - timedelta(days=settings.backfill_days - 1))
    out = []
    day = first
    while day <= newest and len(out) < settings.max_days_per_cycle:
        out.append(day)
        day += timedelta(days=1)
    return out


def monitor_area(conn: sqlite3.Connection, store: Store, provider: Any, aoi: Any,
                 today: date, settings: Settings) -> dict:
    out = {"aoi_id": aoi.id, "done": 0, "skipped": 0, "error": None, "alerts": 0}
    for day in days_to_run(conn, store.org_id, aoi.id, today, settings):
        attempt = ledger.attempts_for(conn, store.org_id, aoi.id, day) + 1
        try:
            result = pipeline.run_day(store, provider, aoi, day)
        except Exception as e:                                # noqa: BLE001
            detail = f"{type(e).__name__}: {e}"
            log.warning("%s %s %s failed (attempt %d): %s", store.org_id, aoi.id,
                        day, attempt, detail)
            log.debug(traceback.format_exc())
            if attempt >= settings.max_day_attempts:
                ledger.record_run(conn, store.org_id, aoi.id, day, "skipped",
                                  attempt, 0, detail)
                out["skipped"] += 1
                continue
            ledger.record_run(conn, store.org_id, aoi.id, day, "error", attempt,
                              0, detail)
            out["error"] = detail
            #: Stop this area for the cycle. Running later days now would
            #: assess them against a history with a hole the retry may fill.
            break
        ledger.record_run(conn, store.org_id, aoi.id, day, "done", attempt,
                          result.alerts, "; ".join(result.notes))
        out["done"] += 1
        out["alerts"] += result.alerts
    return out


def run_cycle(settings: Settings, *, today: date | None = None,
              provider: Any = None, transport: Any = None,
              delivery_kwargs: dict | None = None) -> dict:
    """One pass over every organisation. Safe to run at any time."""
    prepare_database(settings.db_path)
    today = today or datetime.now(timezone.utc).date()
    started = time.monotonic()
    summary: dict[str, Any] = {
        "started_at": ledger.iso(ledger.now()), "today": today.isoformat(),
        "provider": providers.configured_name() if provider is None else
        type(provider).__name__,
        "organisations": 0, "areas": 0, "area_days": 0, "alerts": 0,
        "errors": [], "delivery": {"delivered": 0, "failed": 0,
                                   "suppressed": 0, "deferred": 0}}

    if provider is None:
        try:
            provider = providers.resolve()
        except Exception as e:                                # noqa: BLE001
            summary["errors"].append(f"provider: {type(e).__name__}: {e}")
            provider = None

    conn = ledger.connect(settings.db_path)
    try:
        orgs = [r[0] for r in conn.execute("SELECT id FROM organizations ORDER BY id")]
        summary["organisations"] = len(orgs)
        for org in orgs:
            with Store(settings.db_path, org_id=org, actor=ACTOR,
                       role=Role.ADMIN) as store:
                try:
                    ensure_default_rules(store)
                    if provider is not None:
                        for aoi in store.list_aois():
                            summary["areas"] += 1
                            r = monitor_area(conn, store, provider, aoi, today,
                                             settings)
                            summary["area_days"] += r["done"]
                            summary["alerts"] += r["alerts"]
                            if r["error"]:
                                summary["errors"].append(
                                    f"{org}/{aoi.id}: {r['error']}")
                    sent = delivery.deliver_for_org(
                        conn, store, settings, transport=transport,
                        **(delivery_kwargs or {}))
                    for k, v in sent.items():
                        summary["delivery"][k] += v
                except Exception as e:                        # noqa: BLE001
                    log.exception("organisation %s failed", org)
                    summary["errors"].append(f"{org}: {type(e).__name__}: {e}")
        summary["errors"] = summary["errors"][-50:]
        summary["duration_s"] = round(time.monotonic() - started, 2)
        ledger.heartbeat(conn, summary)
    finally:
        conn.close()
    log.info("cycle: %d org(s), %d area(s), %d area-day(s), %d alert(s), "
             "delivery %s, %d error(s) in %.1fs", summary["organisations"],
             summary["areas"], summary["area_days"], summary["alerts"],
             summary["delivery"], len(summary["errors"]), summary["duration_s"])
    return summary


def run_forever(settings: Settings, stop: threading.Event | None = None) -> None:
    """Run cycles until stopped. Only the lease holder runs a cycle."""
    stop = stop or threading.Event()
    holder = holder_id()
    ttl = settings.worker_interval_s * 2
    log.info("worker %s: every %ss", holder, settings.worker_interval_s)
    while not stop.is_set():
        conn = ledger.connect(settings.db_path)
        try:
            ledger.migrate(conn)
            mine = ledger.acquire_lease(conn, holder, ttl)
        finally:
            conn.close()
        if mine:
            try:
                run_cycle(settings)
            except Exception:                                 # noqa: BLE001
                log.exception("monitoring cycle failed")
        else:
            log.info("another worker holds the lease; standing by")
        stop.wait(settings.worker_interval_s)
    conn = ledger.connect(settings.db_path)
    try:
        ledger.release_lease(conn, holder)
    finally:
        conn.close()
