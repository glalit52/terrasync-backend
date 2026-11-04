"""The service end to end, through ASGI: the engine's contract, unchanged, plus
what this layer adds."""

import pytest

pytest.importorskip("terrashield")

from starlette.testclient import TestClient  # noqa: E402

from conftest import GOOD  # noqa: E402
from terrasync import app as app_module  # noqa: E402
from terrasync.app import create_app  # noqa: E402
from terrasync.config import Settings  # noqa: E402

SQUARE = [[72.98, 20.98], [73.02, 20.98], [73.02, 21.02], [72.98, 21.02],
          [72.98, 20.98]]


@pytest.fixture
def settings(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<!doctype html><html><body>TerraShield</body></html>")
    return Settings(db_path=str(tmp_path / "t.db"), webapp_path=str(page),
                    admin_email="owner@example.com", admin_password=GOOD,
                    admin_org="Sensegrass", auth_rate_limit=5,
                    webhook_allow_private=True)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def login(client, email="owner@example.com", password=GOOD):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_refuses_to_start_without_any_way_to_authenticate(tmp_path):
    s = Settings(db_path=str(tmp_path / "empty.db"))
    with pytest.raises(RuntimeError, match="refusing to serve"):
        with TestClient(create_app(s)):
            pass


def test_probes(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["database"] == "ok"


def test_webapp_is_served_with_policy(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "TerraShield" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-request-id"]


def test_missing_webapp_says_how_to_fix(settings, tmp_path):
    s = Settings(**{**settings.__dict__, "webapp_path": str(tmp_path / "nope.html")})
    with TestClient(create_app(s)) as c:
        r = c.get("/")
    assert r.status_code == 404 and "TERRASYNC_WEBAPP" in r.json()["hint"]


def test_the_web_applications_flow(client):
    """The calls webapp/index.html makes, in the order it makes them."""
    h = login(client)
    me = client.get("/api/me", headers=h).json()
    assert me["role"] == "admin" and me["actor"] == "owner@example.com"

    r = client.post("/api/sites", headers=h,
                    json={"name": "Test block", "boundary": SQUARE})
    assert r.status_code == 200, r.text
    site = r.json()["site"]["id"]

    assert [s["id"] for s in client.get("/api/sites", headers=h).json()["sites"]] == [site]
    assert client.get(f"/api/sites/{site}", headers=h).status_code == 200
    assert client.get(f"/api/sites/{site}/fields", headers=h).status_code == 200
    assert "alerts" in client.get("/api/alerts?limit=200", headers=h).json()
    assert "changes" in client.get("/api/changes?limit=200", headers=h).json()
    assert client.get("/api/health", headers=h).status_code == 200
    assert client.get("/api/audit?limit=200", headers=h).status_code == 200
    r = client.post("/api/copilot", headers=h, json={"question": "what changed?"})
    assert r.status_code == 200

    assert client.post("/api/auth/logout", headers=h).status_code == 200
    assert client.get("/api/me", headers=h).status_code == 401


def test_bootstrap_installs_rules_for_the_first_org(client):
    h = login(client)
    assert len(client.get("/api/rules", headers=h).json()["rules"]) >= 1


def test_authentication_is_required(client):
    assert client.get("/api/sites").status_code == 401
    assert client.get("/api/sites", headers={"Authorization": "Bearer junk"}).status_code == 401
    #: Unknown paths demand a token too, rather than listing what exists.
    assert client.get("/api/no-such-thing").status_code == 401


def test_signup_is_closed_by_default(client):
    r = client.post("/api/auth/signup", json={
        "organisation": "X", "email": "x@example.com", "password": GOOD})
    assert r.status_code == 403


def test_failed_logins_are_throttled_successes_are_not(client):
    for _ in range(8):
        login(client)
    codes = [client.post("/api/auth/login", json={
        "email": "owner@example.com", "password": "wrong-password-123"}).status_code
        for _ in range(7)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_bad_bodies(client, settings):
    h = login(client)
    assert client.post("/api/sites", headers={**h, "Content-Type": "application/json"},
                       content=b"{nope").status_code == 400
    assert client.post("/api/sites", headers=h, json=[1, 2]).status_code == 400
    big = b"{" + b" " * (settings.max_body_bytes + 10) + b"}"
    assert client.post("/api/sites", headers={**h, "Content-Type": "application/json"},
                       content=big).status_code == 413


def test_unsupported_method(client):
    r = client.delete("/api/sites")
    assert r.status_code == 405 and r.headers["allow"] == "GET, POST"


def test_internal_errors_do_not_leak_tracebacks(client, monkeypatch):
    def boom(*a, **k):
        return 500, {"error": "KeyError: secret", "trace": "Traceback ... secret"}
    monkeypatch.setattr(app_module, "dispatch", boom)
    h = login(client)
    r = client.get("/api/sites", headers=h)
    assert r.status_code == 500
    assert "secret" not in r.text and r.json()["request_id"]


def test_destinations_are_admin_only_and_tenant_scoped(settings):
    s = Settings(**{**settings.__dict__, "allow_signup": True})
    with TestClient(create_app(s)) as c:
        h = login(c)
        r = c.post("/api/destinations", headers=h, json={
            "channel": "webhook", "target": "https://10.0.0.5/hook", "min_priority": 2})
        assert r.status_code == 200, r.text
        assert r.json()["signing_secret"]
        dest = r.json()["destination"]["id"]
        listed = c.get("/api/destinations", headers=h).json()["destinations"]
        assert [d["id"] for d in listed] == [dest]
        assert "secret" not in listed[0]

        r = c.post("/api/auth/signup", json={
            "organisation": "Other", "email": "other@example.com", "password": GOOD})
        assert r.status_code == 200
        other = {"Authorization": f"Bearer {r.json()['token']}"}
        assert c.get("/api/destinations", headers=other).json()["destinations"] == []
        assert c.post(f"/api/destinations/{dest}/disable", headers=other).status_code == 404
        assert c.post(f"/api/destinations/{dest}/disable", headers=h).status_code == 200


def test_destination_validation(client):
    h = login(client)
    for body in ({"channel": "sms", "target": "x"},
                 {"channel": "email", "target": "nope"},
                 {"channel": "webhook", "target": "ftp://x"},
                 {"channel": "webhook", "target": "https://x.example", "min_priority": 9}):
        assert client.post("/api/destinations", headers=h, json=body).status_code == 400


def test_operations_routes(client):
    h = login(client)
    assert "providers" in client.get("/api/providers", headers=h).json()
    w = client.get("/api/worker", headers=h).json()
    assert "runs" in w and "heartbeat_at" in w
    assert "deliveries" in client.get("/api/deliveries", headers=h).json()
