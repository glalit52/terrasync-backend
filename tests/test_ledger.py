from datetime import date, datetime, timedelta, timezone

from terrasync import ledger


def conn(tmp_path):
    c = ledger.connect(str(tmp_path / "l.db"))
    ledger.migrate(c)
    return c


def test_destinations_are_tenant_scoped(tmp_path):
    c = conn(tmp_path)
    a = ledger.add_destination(c, "org-a", "webhook", "https://a/", "s", 2, "x")
    ledger.add_destination(c, "org-b", "webhook", "https://b/", "s", 2, "y")
    assert [d.id for d in ledger.list_destinations(c, "org-a")] == [a.id]
    assert not ledger.set_destination_enabled(c, "org-b", a.id, False)
    assert ledger.set_destination_enabled(c, "org-a", a.id, False)
    assert ledger.list_destinations(c, "org-a", enabled_only=True) == []
    assert "secret" not in a.public()


def test_failed_delivery_stays_pending_until_it_has_failed_enough(tmp_path):
    c = conn(tmp_path)
    at = datetime.now(timezone.utc)
    for _ in range(ledger.MAX_FAILED_CYCLES - 1):
        ledger.record_delivery(c, "o", "al-1", "d-1", "failed", 1, 503, "", at)
        assert ("al-1", "d-1") not in ledger.delivered_pairs(c, "o")
    ledger.record_delivery(c, "o", "al-1", "d-1", "failed", 1, 503, "", at)
    assert ("al-1", "d-1") in ledger.delivered_pairs(c, "o")


def test_delivered_and_suppressed_are_final(tmp_path):
    c = conn(tmp_path)
    at = datetime.now(timezone.utc)
    ledger.record_delivery(c, "o", "al-1", "d-1", "delivered", 1, 200, "", at)
    ledger.record_delivery(c, "o", "al-2", "d-1", "suppressed", 0, 0, "", at)
    assert ledger.delivered_pairs(c, "o") == {("al-1", "d-1"), ("al-2", "d-1")}
    assert ledger.delivered_pairs(c, "other") == set()


def test_seen_is_first_sighting_only(tmp_path):
    c = conn(tmp_path)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ledger.mark_seen(c, "o", ["a1"], at=t0)
    ledger.mark_seen(c, "o", ["a1", "a2"], at=t0 + timedelta(days=1))
    seen = ledger.seen_at(c, "o")
    assert seen["a1"].startswith("2026-01-01")
    assert seen["a2"].startswith("2026-01-02")


def test_runs_settle_on_done_or_skipped_not_error(tmp_path):
    c = conn(tmp_path)
    ledger.record_run(c, "o", "aoi", date(2026, 3, 1), "done", 1, 0, "")
    ledger.record_run(c, "o", "aoi", date(2026, 3, 2), "error", 1, 0, "boom")
    assert ledger.last_settled_day(c, "o", "aoi") == date(2026, 3, 1)
    assert ledger.attempts_for(c, "o", "aoi", date(2026, 3, 2)) == 1
    ledger.record_run(c, "o", "aoi", date(2026, 3, 2), "skipped", 3, 0, "boom")
    assert ledger.last_settled_day(c, "o", "aoi") == date(2026, 3, 2)


def test_lease_is_exclusive_until_it_expires(tmp_path):
    c = conn(tmp_path)
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ledger.acquire_lease(c, "w1", 60, at=t)
    assert not ledger.acquire_lease(c, "w2", 60, at=t + timedelta(seconds=30))
    assert ledger.acquire_lease(c, "w1", 60, at=t + timedelta(seconds=30))
    assert ledger.acquire_lease(c, "w2", 60, at=t + timedelta(seconds=200))
    ledger.release_lease(c, "w2")
    assert ledger.acquire_lease(c, "w3", 60, at=t + timedelta(seconds=201))


def test_worker_state_roundtrip(tmp_path):
    c = conn(tmp_path)
    assert ledger.worker_state(c)["heartbeat_at"] is None
    ledger.heartbeat(c, {"areas": 3})
    s = ledger.worker_state(c)
    assert s["heartbeat_at"] and s["last_cycle"] == {"areas": 3}
