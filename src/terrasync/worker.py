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


def _published_at(provider: Any, scene: Any) -> datetime | None:
    """When the provider published the scene, from its STAC item, if known.

    Read from the items the provider remembered during search, never by a
    fresh request: this is bookkeeping and must not cost a call per scene.
    """
    items = getattr(provider, "_items", None) or {}
    props = (items.get(scene.id) or {}).get("properties") or {}
    for key in ("published", "created"):
        value = props.get(key)
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def watch_arrivals(conn: sqlite3.Connection, store: Store, provider: Any, aoi: Any,
                   today: date, settings: Settings) -> set[date]:
    """Record newly published scenes; return already-processed days to redo.

    Providers publish hours to days after acquisition, so a day analysed
    before its imagery appeared would otherwise keep the answer "no
    acquisition" forever. A new usable scene on a day already processed puts
    that day back in the queue; the engine rewrites a day's findings in place
    and suppresses alerts it has already raised, so a re-run adds what is new
    and repeats nothing.
    """
    if settings.recheck_days <= 0:
        return set()
    start = today - timedelta(days=settings.recheck_days)
    scenes = provider.search(aoi, start, today)
    known = ledger.known_scene_ids(conn, store.org_id, aoi.id)
    #: The first time an area is watched, everything in the window is "new"
    #: only because nobody was looking. Record it, re-run nothing. Marked
    #: after the search succeeds, so a failed first look is not the one used.
    first_watch = ledger.start_watch(conn, store.org_id, aoi.id)
    done = ledger.done_days(conn, store.org_id, aoi.id)
    redo: set[date] = set()
    for scene in scenes:
        if scene.id in known:
            continue
        ledger.record_scene(conn, store.org_id, aoi.id, scene.id,
                            scene.constellation.value, scene.usable,
                            scene.acquired_at, _published_at(provider, scene))
        if not first_watch and scene.usable and scene.acquired_on in done:
            log.info("%s %s: %s published after %s was processed; re-running",
                     store.org_id, aoi.id, scene.id, scene.acquired_on)
            redo.add(scene.acquired_on)
    return redo


def _note_processed(conn: sqlite3.Connection, store: Store, provider: Any,
                    aoi: Any, result: Any) -> None:
    scene_id = getattr(result, "scene_used", "") or ""
    if not scene_id:
        return
    if scene_id not in ledger.known_scene_ids(conn, store.org_id, aoi.id):
        #: Backfilled days are older than the watch window; record the scene
        #: so its processing time is still on the record.
        for s in store.list_scenes(aoi.id, result.when, result.when):
            if s.id == scene_id:
                ledger.record_scene(conn, store.org_id, aoi.id, s.id,
                                    s.constellation.value, s.usable,
                                    s.acquired_at, _published_at(provider, s))
    ledger.mark_processed(conn, store.org_id, aoi.id, scene_id)


def monitor_area(conn: sqlite3.Connection, store: Store, provider: Any, aoi: Any,
                 today: date, settings: Settings) -> dict:
    out = {"aoi_id": aoi.id, "done": 0, "skipped": 0, "errors": [], "alerts": 0,
           "rerun": 0}
    try:
        redo = watch_arrivals(conn, store, provider, aoi, today, settings)
    except Exception as e:                                    # noqa: BLE001
        redo = set()
        out["errors"].append(f"watching for new scenes: {type(e).__name__}: {e}")
    days = sorted(set(days_to_run(conn, store.org_id, aoi.id, today, settings))
                  | redo)[:settings.max_days_per_cycle]
    for day in days:
        previous = ledger.run_status(conn, store.org_id, aoi.id, day)
        attempt = (1 if previous == "done"
                   else ledger.attempts_for(conn, store.org_id, aoi.id, day) + 1)
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
                #: Given up on is still a failure. Reporting only retryable
                #: errors would let a provider that can never read pixels look
                #: like a clean cycle with no imagery.
                out["errors"].append(
                    f"{day} skipped after {attempt} attempt(s): {detail}")
                continue
            ledger.record_run(conn, store.org_id, aoi.id, day, "error", attempt,
                              0, detail)
            out["errors"].append(f"{day}: {detail}")
            #: Stop this area for the cycle. Running later days now would
            #: assess them against a history with a hole the retry may fill.
            break
        ledger.record_run(conn, store.org_id, aoi.id, day, "done", attempt,
                          result.alerts, "; ".join(result.notes))
        _note_processed(conn, store, provider, aoi, result)
        out["done"] += 1
        out["rerun"] += 1 if day in redo else 0
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
        "organisations": 0, "areas": 0, "area_days": 0, "reruns": 0, "alerts": 0,
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
                            summary["reruns"] += r["rerun"]
                            summary["alerts"] += r["alerts"]
                            summary["errors"].extend(
                                f"{org}/{aoi.id}: {e}" for e in r["errors"])
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
