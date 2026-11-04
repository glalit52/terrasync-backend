"""The backend's own tables, kept beside the engine's in the same database.

Prefixed `terrasync_` so they can never collide with an engine migration, and
created with IF NOT EXISTS so they need no version table of their own. Every
row carries `org_id`, and every query here filters on it: the same tenant rule
the engine enforces in its store.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS terrasync_destinations (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    target       TEXT NOT NULL,
    secret       TEXT NOT NULL DEFAULT '',
    min_priority INTEGER NOT NULL DEFAULT 2,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tsd_org ON terrasync_destinations(org_id);

-- When this service first saw an alert. Deliveries are decided against this,
-- not the alert's own timestamp, which is the acquisition time and can be days
-- before the alert existed.
CREATE TABLE IF NOT EXISTS terrasync_alert_seen (
    org_id   TEXT NOT NULL,
    alert_id TEXT NOT NULL,
    seen_at  TEXT NOT NULL,
    PRIMARY KEY (org_id, alert_id)
);

CREATE TABLE IF NOT EXISTS terrasync_deliveries (
    org_id         TEXT NOT NULL,
    alert_id       TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    status         INTEGER NOT NULL DEFAULT 0,
    detail         TEXT NOT NULL DEFAULT '',
    at             TEXT NOT NULL,
    cycles         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (alert_id, destination_id)
);
CREATE INDEX IF NOT EXISTS idx_tsdel_org ON terrasync_deliveries(org_id, at);

CREATE TABLE IF NOT EXISTS terrasync_runs (
    org_id      TEXT NOT NULL,
    aoi_id      TEXT NOT NULL,
    day         TEXT NOT NULL,
    status      TEXT NOT NULL,          -- done | error | skipped
    attempts    INTEGER NOT NULL DEFAULT 1,
    alerts      INTEGER NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL,
    PRIMARY KEY (org_id, aoi_id, day)
);

CREATE TABLE IF NOT EXISTS terrasync_worker (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    holder       TEXT NOT NULL DEFAULT '',
    lease_until  TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT NOT NULL DEFAULT '',
    last_cycle   TEXT NOT NULL DEFAULT '{}'
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO terrasync_worker (id) VALUES (1)")
    conn.commit()


def connect(db_path: str) -> sqlite3.Connection:
    """A connection tuned for one writer process and one reader process.

    WAL lets the API read while the worker writes, and the busy timeout turns
    a brief writer collision into a short wait instead of an error.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# -- destinations ------------------------------------------------------------

@dataclass(frozen=True)
class DestinationRow:
    id: str
    org_id: str
    channel: str
    target: str
    secret: str
    min_priority: int
    enabled: bool
    created_by: str
    created_at: str

    def public(self) -> dict:
        """Never includes the secret: it is shown once, at creation."""
        return {"id": self.id, "channel": self.channel, "target": self.target,
                "min_priority": self.min_priority, "enabled": self.enabled,
                "created_by": self.created_by, "created_at": self.created_at}


def _dest(r: sqlite3.Row) -> DestinationRow:
    return DestinationRow(r["id"], r["org_id"], r["channel"], r["target"],
                          r["secret"], int(r["min_priority"]), bool(r["enabled"]),
                          r["created_by"], r["created_at"])


def add_destination(conn: sqlite3.Connection, org_id: str, channel: str,
                    target: str, secret: str, min_priority: int,
                    created_by: str) -> DestinationRow:
    row = DestinationRow(f"dst-{uuid.uuid4().hex[:12]}", org_id, channel, target,
                         secret, min_priority, True, created_by, iso(now()))
    with conn:
        conn.execute(
            "INSERT INTO terrasync_destinations (id, org_id, channel, target, "
            "secret, min_priority, enabled, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (row.id, org_id, channel, target, secret, min_priority, created_by,
             row.created_at))
    return row


def list_destinations(conn: sqlite3.Connection, org_id: str,
                      enabled_only: bool = False) -> list[DestinationRow]:
    sql = "SELECT * FROM terrasync_destinations WHERE org_id = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    return [_dest(r) for r in conn.execute(sql + " ORDER BY created_at",
                                           (org_id,)).fetchall()]


def set_destination_enabled(conn: sqlite3.Connection, org_id: str,
                            destination_id: str, enabled: bool) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE terrasync_destinations SET enabled = ? "
            "WHERE id = ? AND org_id = ?",
            (1 if enabled else 0, destination_id, org_id))
    return cur.rowcount == 1


# -- alerts seen and deliveries ---------------------------------------------

def mark_seen(conn: sqlite3.Connection, org_id: str, alert_ids: list[str],
              at: datetime | None = None) -> None:
    stamp = iso(at or now())
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO terrasync_alert_seen (org_id, alert_id, seen_at)"
            " VALUES (?,?,?)", [(org_id, a, stamp) for a in alert_ids])


def seen_at(conn: sqlite3.Connection, org_id: str) -> dict[str, str]:
    return {r["alert_id"]: r["seen_at"] for r in conn.execute(
        "SELECT alert_id, seen_at FROM terrasync_alert_seen WHERE org_id = ?",
        (org_id,)).fetchall()}


#: Cycles a failing delivery is retried on before it is left as failed. One
#: attempt per cycle, so a dead endpoint costs one request per cycle, not one
#: per pending alert.
MAX_FAILED_CYCLES = 3


def delivered_pairs(conn: sqlite3.Connection, org_id: str) -> set[tuple[str, str]]:
    """(alert, destination) pairs that are finished with.

    Delivered and suppressed are final. Failed is final only after it has
    failed on MAX_FAILED_CYCLES cycles; before that it is still pending.
    """
    return {(r["alert_id"], r["destination_id"]) for r in conn.execute(
        "SELECT alert_id, destination_id FROM terrasync_deliveries "
        "WHERE org_id = ? AND (outcome != 'failed' OR cycles >= ?)",
        (org_id, MAX_FAILED_CYCLES)).fetchall()}


def record_delivery(conn: sqlite3.Connection, org_id: str, alert_id: str,
                    destination_id: str, outcome: str, attempts: int,
                    status: int, detail: str, at: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT INTO terrasync_deliveries (org_id, alert_id, destination_id, "
            "outcome, attempts, status, detail, at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (alert_id, destination_id) DO UPDATE SET "
            "outcome = excluded.outcome, attempts = excluded.attempts, "
            "status = excluded.status, detail = excluded.detail, at = excluded.at, "
            "cycles = terrasync_deliveries.cycles + 1",
            (org_id, alert_id, destination_id, outcome, attempts, status,
             detail[:500], iso(at)))


def list_deliveries(conn: sqlite3.Connection, org_id: str,
                    limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT alert_id, destination_id, outcome, attempts, status, detail, at, "
        "cycles FROM terrasync_deliveries WHERE org_id = ? ORDER BY at DESC LIMIT ?",
        (org_id, limit)).fetchall()
    return [dict(r) for r in rows]


# -- monitoring runs ---------------------------------------------------------

def last_settled_day(conn: sqlite3.Connection, org_id: str,
                     aoi_id: str) -> date | None:
    """The latest day that is finished with, whether done or given up on."""
    r = conn.execute(
        "SELECT MAX(day) FROM terrasync_runs WHERE org_id = ? AND aoi_id = ? "
        "AND status IN ('done', 'skipped')", (org_id, aoi_id)).fetchone()
    return date.fromisoformat(r[0]) if r and r[0] else None


def attempts_for(conn: sqlite3.Connection, org_id: str, aoi_id: str,
                 day: date) -> int:
    r = conn.execute(
        "SELECT attempts FROM terrasync_runs WHERE org_id = ? AND aoi_id = ? "
        "AND day = ?", (org_id, aoi_id, day.isoformat())).fetchone()
    return int(r[0]) if r else 0


def record_run(conn: sqlite3.Connection, org_id: str, aoi_id: str, day: date,
               status: str, attempts: int, alerts: int, detail: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO terrasync_runs (org_id, aoi_id, day, status, "
            "attempts, alerts, detail, finished_at) VALUES (?,?,?,?,?,?,?,?)",
            (org_id, aoi_id, day.isoformat(), status, attempts, alerts,
             detail[:1000], iso(now())))


def recent_runs(conn: sqlite3.Connection, org_id: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT aoi_id, day, status, attempts, alerts, detail, finished_at "
        "FROM terrasync_runs WHERE org_id = ? ORDER BY finished_at DESC LIMIT ?",
        (org_id, limit)).fetchall()
    return [dict(r) for r in rows]


# -- worker lease and heartbeat ----------------------------------------------

def acquire_lease(conn: sqlite3.Connection, holder: str, ttl_s: int,
                  at: datetime | None = None) -> bool:
    """Take the single-runner lease if it is free, expired, or already ours.

    Two workers -- an embedded one and a separate service, or two replicas --
    would otherwise both run every area-day. Monitoring is idempotent per day,
    so that would not corrupt anything, but it would double the imagery bill.
    """
    t = at or now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute(
            "SELECT holder, lease_until FROM terrasync_worker WHERE id = 1"
        ).fetchone()
        current, until = (r[0], r[1]) if r else ("", "")
        free = (not until or current == holder
                or datetime.fromisoformat(until) <= t)
        if free:
            conn.execute(
                "UPDATE terrasync_worker SET holder = ?, lease_until = ? WHERE id = 1",
                (holder, iso(t + timedelta(seconds=ttl_s))))
        conn.execute("COMMIT")
        return free
    except Exception:
        conn.execute("ROLLBACK")
        raise


def release_lease(conn: sqlite3.Connection, holder: str) -> None:
    with conn:
        conn.execute("UPDATE terrasync_worker SET lease_until = '' "
                     "WHERE id = 1 AND holder = ?", (holder,))


def heartbeat(conn: sqlite3.Connection, summary: dict) -> None:
    with conn:
        conn.execute(
            "UPDATE terrasync_worker SET heartbeat_at = ?, last_cycle = ? WHERE id = 1",
            (iso(now()), json.dumps(summary, default=str)))


def worker_state(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT holder, lease_until, heartbeat_at, last_cycle "
                     "FROM terrasync_worker WHERE id = 1").fetchone()
    if r is None:
        return {"heartbeat_at": None, "last_cycle": {}}
    return {"heartbeat_at": r["heartbeat_at"] or None,
            "lease_until": r["lease_until"] or None,
            "last_cycle": json.loads(r["last_cycle"] or "{}")}
