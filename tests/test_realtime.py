"""New imagery is processed when it is published, not only when its day comes up.

Providers publish scenes hours to days after acquisition. These tests stand a
provider in front of the modelled estate that withholds a day's scenes and
then "publishes" them, which is exactly what a real catalogue does to the
worker, and check that the late scene is found, its day re-run, its findings
produced, its alerts delivered once, and its latency recorded.
"""

import dataclasses
from datetime import date, datetime, timedelta, timezone

import pytest

pytest.importorskip("terrashield")

from terrashield import pipeline, providers, sites  # noqa: E402
from terrashield.domain import Role  # noqa: E402
from terrashield.store import Store  # noqa: E402

from conftest import GOOD  # noqa: E402
from terrasync import ledger, worker  # noqa: E402
from terrasync.config import Settings  # noqa: E402
from terrasync.provisioning import ensure_admin, prepare_database  # noqa: E402

SITE = "IN-BHD-SOLAR"
TODAY = date(2026, 3, 24)


class LatePublisher:
    """The modelled estate, with some days' scenes not yet published."""

    def __init__(self, inner):
        self.inner = inner
        self.unpublished: set[date] = set()

    def search(self, aoi, start, end, constellations=()):
        found = (self.inner.search(aoi, start, end, constellations)
                 if constellations else self.inner.search(aoi, start, end))
        return [s for s in found if s.acquired_on not in self.unpublished]

    def fetch(self, aoi, scene, gsd_m=None):
        return self.inner.fetch(aoi, scene, gsd_m)

    def masks(self, aoi, scene, r):
        return self.inner.masks(aoi, scene, r)


@pytest.fixture
def env(tmp_path):
    s = Settings(db_path=str(tmp_path / "rt.db"), admin_email="owner@example.com",
                 admin_password=GOOD, backfill_days=4, settle_days=0,
                 recheck_days=3, max_days_per_cycle=10, max_day_attempts=2)
    prepare_database(s.db_path)
    org = ensure_admin(s)
    demo = next(d for d in sites.load_all() if d.aoi.id == SITE)
    aoi = dataclasses.replace(demo.aoi, org_id=org)
    with Store(s.db_path, org_id=org, actor="t", role=Role.ADMIN) as store:
        store.put_aoi(aoi)
    return s, org, aoi


def latest_usable_day(provider, aoi) -> date:
    """The last day in the watch window with a usable pass, optical or SAR."""
    scenes = provider.search(aoi, TODAY - timedelta(days=3), TODAY)
    days = [x.acquired_on for x in scenes if x.usable]
    assert days, "the modelled estate should have a usable pass in the window"
    return max(days)


def run_detail(s, org, day):
    conn = ledger.connect(s.db_path)
    try:
        return {r["day"]: r for r in ledger.recent_runs(conn, org, 100)}[day.isoformat()]
    finally:
        conn.close()


def test_a_late_scene_reruns_its_day_and_is_timestamped(env):
    s, org, aoi = env
    provider = LatePublisher(providers.resolve("synthetic"))
    late_day = latest_usable_day(provider, aoi)
    provider.unpublished.add(late_day)

    first = worker.run_cycle(s, today=TODAY, provider=provider)
    assert first["errors"] == [], first["errors"]
    #: Everything in the window is new the first time, because nobody was
    #: looking -- that is not a reason to re-run anything.
    assert first["reruns"] == 0
    assert "no acquisition" in run_detail(s, org, late_day)["detail"]

    provider.unpublished.clear()
    second = worker.run_cycle(s, today=TODAY, provider=provider)
    assert second["errors"] == [], second["errors"]
    assert second["reruns"] == 1
    assert "no acquisition" not in run_detail(s, org, late_day)["detail"]

    conn = ledger.connect(s.db_path)
    late = [x for x in ledger.recent_scenes(conn, org)
            if x["acquired_at"].startswith(late_day.isoformat()) and x["usable"]]
    summary = ledger.latency_summary(conn, org)
    conn.close()
    assert any(x["processed_at"] for x in late), late
    assert summary["scenes_processed"] >= 1
    assert summary["median_processed_lag_h"] is not None

    #: Nothing new since: nothing re-run.
    third = worker.run_cycle(s, today=TODAY, provider=provider)
    assert third["reruns"] == 0


def test_today_is_processed_the_same_day(env):
    """With no settle delay, today's imagery is analysed in today's cycle."""
    s, org, aoi = env
    worker.run_cycle(s, today=TODAY, provider=providers.resolve("synthetic"))
    assert run_detail(s, org, TODAY)["status"] == "done"


def test_rerunning_a_day_does_not_duplicate_findings_or_alerts(env):
    """The property re-running relies on: the engine rewrites a day in place."""
    s, org, aoi = env
    provider = providers.resolve("synthetic")
    with Store(s.db_path, org_id=org, actor="t", role=Role.ADMIN) as store:
        from terrasync.provisioning import ensure_default_rules
        ensure_default_rules(store)
        #: Bhadla commissions an array block on 2026-03-19; run across it so
        #: there is something to find.
        pipeline.run_range(store, provider, aoi, date(2026, 3, 10), date(2026, 3, 21))

        def counts():
            return (len(store.list_alerts(limit=1000)),
                    len(store.list_changes(aoi.id)),
                    len(store.list_detections(aoi.id)))

        before = counts()
        for day in (date(2026, 3, 19), date(2026, 3, 20), date(2026, 3, 21)):
            pipeline.run_day(store, provider, aoi, day)
        assert counts() == before


def test_the_published_time_comes_from_the_items_the_search_returned():
    class P:
        _items = {"S2A_X": {"properties": {"created": "2026-03-20T09:30:00Z"}}}

    class Sc:
        id = "S2A_X"

    assert worker._published_at(P(), Sc()) == datetime(2026, 3, 20, 9, 30,
                                                      tzinfo=timezone.utc)
    assert worker._published_at(object(), Sc()) is None


def test_a_catalogue_failure_while_watching_is_reported_not_fatal(env):
    s, org, aoi = env

    class Down:
        def search(self, *a, **k):
            raise RuntimeError("catalogue unavailable")

    summary = worker.run_cycle(s, today=TODAY, provider=Down())
    assert any("watching for new scenes" in e for e in summary["errors"])


def test_a_late_scene_with_a_new_structure_is_alerted_in_the_cycle_it_arrives(tmp_path):
    """The whole point: a scene published late still produces an alert, and
    the alert reaches the customer's webhook in the same cycle, signed."""
    import json
    from terrashield.delivery import verify
    from terrashield.providers.transport import Response

    s = Settings(db_path=str(tmp_path / "e2e.db"), admin_email="owner@example.com",
                 admin_password=GOOD, backfill_days=12, settle_days=0,
                 recheck_days=3, max_days_per_cycle=20)
    prepare_database(s.db_path)
    org = ensure_admin(s)
    demo = next(d for d in sites.load_all() if d.aoi.id == SITE)
    aoi = dataclasses.replace(demo.aoi, org_id=org)
    with Store(s.db_path, org_id=org, actor="t", role=Role.ADMIN) as store:
        store.put_aoi(aoi)

    #: Bhadla's new array block is first visible on the 2026-03-20 pass.
    event_day = date(2026, 3, 20)
    provider = LatePublisher(providers.resolve("synthetic"))
    provider.unpublished.add(event_day)
    first = worker.run_cycle(s, today=event_day, provider=provider)
    assert first["alerts"] == 0

    conn = ledger.connect(s.db_path)
    ledger.add_destination(conn, org, "webhook", "https://hooks.example/ts",
                           "sekrit", 2, "owner")
    conn.close()

    sent = []

    class Recorder:
        def request(self, method, url, *, headers=None, body=None, timeout=30.0):
            sent.append((headers, body))
            return Response(200, b"", {})

    provider.unpublished.clear()
    second = worker.run_cycle(
        s, today=event_day, provider=provider, transport=Recorder(),
        delivery_kwargs={"resolver": lambda host: ["93.184.216.34"]})
    assert second["reruns"] == 1
    assert second["alerts"] >= 1
    assert second["delivery"]["delivered"] >= 1
    headers, body = sent[0]
    assert verify(body, headers["X-TerraShield-Signature"], "sekrit")
    assert json.loads(body)["aoi_id"] == SITE
