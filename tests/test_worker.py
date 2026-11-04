import dataclasses
from datetime import date

import pytest

pytest.importorskip("terrashield")

from terrashield import providers, sites  # noqa: E402
from terrashield.domain import Role  # noqa: E402
from terrashield.store import Store  # noqa: E402

from conftest import GOOD  # noqa: E402
from terrasync import ledger, worker  # noqa: E402
from terrasync.config import Settings  # noqa: E402
from terrasync.provisioning import (ensure_admin, ensure_default_rules,  # noqa: E402
                                    prepare_database)


@pytest.fixture
def env(tmp_path):
    s = Settings(db_path=str(tmp_path / "w.db"), admin_email="owner@example.com",
                 admin_password=GOOD, backfill_days=3, settle_days=1,
                 max_days_per_cycle=7, max_day_attempts=2)
    prepare_database(s.db_path)
    org = ensure_admin(s)
    return s, org


def enrol(s, org, aoi_id="IN-BHD-SOLAR"):
    demo = next(d for d in sites.load_all() if d.aoi.id == aoi_id)
    aoi = dataclasses.replace(demo.aoi, org_id=org)
    with Store(s.db_path, org_id=org, actor="t", role=Role.ADMIN) as store:
        store.put_aoi(aoi)
    return aoi


def test_days_to_run(env, tmp_path):
    s, org = env
    conn = ledger.connect(s.db_path)
    today = date(2026, 3, 22)
    assert worker.days_to_run(conn, org, "a", today, s) == [
        date(2026, 3, 19), date(2026, 3, 20), date(2026, 3, 21)]
    ledger.record_run(conn, org, "a", date(2026, 3, 20), "done", 1, 0, "")
    assert worker.days_to_run(conn, org, "a", today, s) == [date(2026, 3, 21)]
    conn.close()


def test_cycle_monitors_enrolled_areas(env):
    s, org = env
    enrol(s, org)
    summary = worker.run_cycle(s, today=date(2026, 3, 22),
                               provider=providers.resolve("synthetic"))
    assert summary["errors"] == [], summary["errors"]
    assert summary["area_days"] == 3
    conn = ledger.connect(s.db_path)
    runs = ledger.recent_runs(conn, org)
    assert {r["status"] for r in runs} == {"done"}
    assert ledger.worker_state(conn)["heartbeat_at"]
    conn.close()
    #: The next cycle on the same day has nothing left to do.
    again = worker.run_cycle(s, today=date(2026, 3, 22),
                             provider=providers.resolve("synthetic"))
    assert again["area_days"] == 0


def test_failing_day_blocks_then_is_skipped(env):
    s, org = env
    enrol(s, org)

    class Down:
        def search(self, *a, **k):
            raise RuntimeError("catalogue unavailable")

    first = worker.run_cycle(s, today=date(2026, 3, 22), provider=Down())
    assert any("catalogue unavailable" in e for e in first["errors"])
    conn = ledger.connect(s.db_path)
    assert ledger.last_settled_day(conn, org, "IN-BHD-SOLAR") is None
    worker.run_cycle(s, today=date(2026, 3, 22), provider=Down())
    #: Two attempts on the first day -> skipped, and the area moves on.
    assert ledger.last_settled_day(conn, org, "IN-BHD-SOLAR") == date(2026, 3, 19)
    conn.close()


def test_default_rules_are_per_organisation(env):
    s, org_a = env
    s_b = Settings(**{**s.__dict__, "admin_email": "b@example.com", "admin_org": "B"})
    org_b = ensure_admin(s_b)
    with Store(s.db_path, org_id=org_a, actor="t", role=Role.ADMIN) as a, \
         Store(s.db_path, org_id=org_b, actor="t", role=Role.ADMIN) as b:
        assert len(a.list_rules()) == len(b.list_rules()) > 0
        assert ensure_default_rules(a) == 0          # idempotent
        assert {r.id for r in a.list_rules()}.isdisjoint({r.id for r in b.list_rules()})
