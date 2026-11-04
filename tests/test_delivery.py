import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("terrashield")

from terrashield.delivery import verify  # noqa: E402
from terrashield.domain import Alert, Aoi, AoiKind, Role, Severity  # noqa: E402
from terrashield.providers.transport import Response, TransportError  # noqa: E402
from terrashield.store import Store  # noqa: E402

from conftest import GOOD  # noqa: E402
from terrasync import ledger  # noqa: E402
from terrasync.config import Settings  # noqa: E402
from terrasync.delivery import deliver_for_org  # noqa: E402
from terrasync.provisioning import ensure_admin, prepare_database  # noqa: E402

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


class Recorder:
    def __init__(self, fail_with=None):
        self.sent = []
        self.fail_with = fail_with

    def request(self, method, url, *, headers=None, body=None, timeout=30.0):
        self.sent.append((url, headers, body))
        if self.fail_with:
            raise self.fail_with
        return Response(200, b"")


@pytest.fixture
def env(tmp_path):
    s = Settings(db_path=str(tmp_path / "d.db"), admin_email="owner@example.com",
                 admin_password=GOOD, public_base_url="https://ts.example.com")
    prepare_database(s.db_path)
    org = ensure_admin(s)
    store = Store(s.db_path, org_id=org, actor="test", role=Role.ADMIN)
    store.put_aoi(Aoi(id="aoi-1", org_id=org, name="Test", kind=AoiKind.GENERIC,
                      boundary=[(72.98, 20.98), (73.02, 20.98), (73.02, 21.02),
                                (72.98, 21.02), (72.98, 20.98)]))
    conn = ledger.connect(s.db_path)
    yield s, store, conn
    store.close()
    conn.close()


def raise_alert(store, alert_id, priority=1):
    store.put_alert(Alert(id=alert_id, org_id=store.org_id, aoi_id="aoi-1",
                          rule_id="rule-x", title="secret title",
                          severity=Severity.HIGH, priority=priority,
                          created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
                          summary="secret summary"), {"lat": 21.0})


def test_new_alert_is_delivered_signed_and_redacted_once(env):
    s, store, conn = env
    d = ledger.add_destination(conn, store.org_id, "webhook", "https://hooks.example/x",
                               "sekrit", 2, "owner")
    raise_alert(store, "al-1")
    t = Recorder()
    out = deliver_for_org(conn, store, s, transport=t, resolver=PUBLIC)
    assert out["delivered"] == 1
    url, headers, body = t.sent[0]
    assert verify(body, headers["X-TerraShield-Signature"], "sekrit")
    payload = json.loads(body)
    assert payload["alert_id"] == "al-1"
    assert payload["url"].startswith("https://ts.example.com")
    assert "secret" not in body.decode()
    assert deliver_for_org(conn, store, s, transport=t, resolver=PUBLIC)["delivered"] == 0
    assert len(t.sent) == 1
    assert ledger.list_deliveries(conn, store.org_id)[0]["destination_id"] == d.id


def test_history_is_not_sent_to_a_new_destination(env):
    s, store, conn = env
    raise_alert(store, "al-old")
    deliver_for_org(conn, store, s, transport=Recorder(), resolver=PUBLIC)
    ledger.add_destination(conn, store.org_id, "webhook", "https://hooks.example/x",
                           "sekrit", 4, "owner")
    t = Recorder()
    deliver_for_org(conn, store, s, transport=t, resolver=PUBLIC)
    assert t.sent == []


def test_low_priority_is_suppressed_not_sent(env):
    s, store, conn = env
    ledger.add_destination(conn, store.org_id, "webhook", "https://hooks.example/x",
                           "sekrit", 1, "owner")
    raise_alert(store, "al-low", priority=3)
    t = Recorder()
    out = deliver_for_org(conn, store, s, transport=t, resolver=PUBLIC)
    assert out["suppressed"] == 1 and t.sent == []


def test_failure_is_recorded_and_retried_next_cycle(env):
    s, store, conn = env
    ledger.add_destination(conn, store.org_id, "webhook", "https://hooks.example/x",
                           "sekrit", 2, "owner")
    raise_alert(store, "al-1")
    raise_alert(store, "al-2")
    down = Recorder(fail_with=TransportError("unavailable", 503))
    out = deliver_for_org(conn, store, s, transport=down, resolver=PUBLIC,
                          sleep=lambda _s: None)
    #: One failure stops the destination for the cycle.
    assert out["failed"] == 1 and out["delivered"] == 0
    up = Recorder()
    out = deliver_for_org(conn, store, s, transport=up, resolver=PUBLIC)
    assert out["delivered"] == 2


def test_private_target_is_refused_at_send_time(env):
    s, store, conn = env
    ledger.add_destination(conn, store.org_id, "webhook", "https://rebound.example/x",
                           "sekrit", 2, "owner")
    raise_alert(store, "al-1")
    t = Recorder()
    out = deliver_for_org(conn, store, s, transport=t,
                          resolver=lambda h: ["169.254.169.254"])
    assert out["failed"] == 1 and t.sent == []


def test_email_without_relay_fails_loudly(env):
    s, store, conn = env
    ledger.add_destination(conn, store.org_id, "email", "ops@example.com", "", 2, "owner")
    raise_alert(store, "al-1")
    out = deliver_for_org(conn, store, s, resolver=PUBLIC)
    assert out["failed"] == 1
    assert "SMTP" in ledger.list_deliveries(conn, store.org_id)[0]["detail"]


def test_email_with_relay(env):
    s, store, conn = env
    from terrasync.config import SmtpSettings
    s = Settings(**{**s.__dict__, "smtp": SmtpSettings(
        host="smtp.example.com", sender="alerts@example.com", username="u",
        password="p")})
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            sent.append(msg)

    ledger.add_destination(conn, store.org_id, "email", "ops@example.com", "", 2, "owner")
    raise_alert(store, "al-1")
    out = deliver_for_org(conn, store, s, smtp_factory=FakeSMTP, resolver=PUBLIC)
    assert out["delivered"] == 1
    assert sent[0]["To"] == "ops@example.com"
    assert "secret" not in sent[0].get_content()
