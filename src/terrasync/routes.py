"""Routes this service adds beside the engine's 28.

Same calling convention as the engine's handlers -- an authenticated `Store`
opened as the caller, plus params and body -- so identity, tenant and role
come from the same place and a handler here cannot choose the organisation.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Callable

from terrashield import providers, rbac
from terrashield.delivery import Channel, Destination, Outcome
from terrashield.store import Store

from . import ledger
from .config import Settings
from .delivery import ServiceDispatcher
from .targets import TargetError, check_email, check_webhook

Handler = Callable[..., Any]
ROUTES: list[tuple[str, "re.Pattern[str]", Handler]] = []


class RouteError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def wrap(fn: Handler) -> Handler:
        ROUTES.append((method, regex, fn))
        return fn
    return wrap


def match(method: str, path: str) -> tuple[Handler, dict] | None:
    for verb, regex, fn in ROUTES:
        if verb == method:
            m = regex.match(path)
            if m:
                return fn, m.groupdict()
    return None


def _one(params: dict, key: str, default: str = "") -> str:
    v = params.get(key)
    return v[0] if isinstance(v, list) and v else (v or default)


def _conn(settings: Settings):
    return ledger.connect(settings.db_path)


# -- destinations -------------------------------------------------------------

@route("GET", "/api/destinations")
def list_destinations(store: Store, settings: Settings, **_) -> dict:
    rbac.require(store.role, "org.configure", store.actor)
    conn = _conn(settings)
    try:
        rows = ledger.list_destinations(conn, store.org_id)
    finally:
        conn.close()
    return {"destinations": [d.public() for d in rows],
            "email_relay_configured": settings.smtp.configured}


@route("POST", "/api/destinations")
def create_destination(store: Store, settings: Settings, body: dict, **_) -> dict:
    rbac.require(store.role, "org.configure", store.actor)
    channel = str(body.get("channel", "webhook")).strip().lower()
    target = str(body.get("target", "")).strip()
    try:
        min_priority = int(body.get("min_priority", 2))
    except (TypeError, ValueError):
        raise RouteError(400, "min_priority must be 1-4") from None
    if not 1 <= min_priority <= 4:
        raise RouteError(400, "min_priority must be 1-4")
    try:
        if channel == Channel.WEBHOOK.value:
            target = check_webhook(target,
                                   allow_private=settings.webhook_allow_private)
        elif channel == Channel.EMAIL.value:
            target = check_email(target)
        else:
            raise RouteError(400, "channel must be webhook or email")
    except TargetError as e:
        raise RouteError(400, str(e)) from None

    secret = secrets.token_urlsafe(32) if channel == Channel.WEBHOOK.value else ""
    conn = _conn(settings)
    try:
        row = ledger.add_destination(conn, store.org_id, channel, target, secret,
                                     min_priority, store.actor)
    finally:
        conn.close()
    store.audit("destination.create", row.id,
                {"channel": channel, "min_priority": min_priority})
    out = {"destination": row.public()}
    if secret:
        out["signing_secret"] = secret
        out["note"] = ("shown once. Verify each delivery's X-TerraShield-Signature "
                       "header with terrashield.delivery.verify(body, header, "
                       "secret)")
    elif not settings.smtp.configured:
        out["note"] = ("no SMTP relay is configured; deliveries to this address "
                       "will be recorded as failed until TERRASYNC_SMTP_HOST and "
                       "TERRASYNC_SMTP_FROM are set")
    return out


def _toggle(store: Store, settings: Settings, destination_id: str,
            enabled: bool) -> dict:
    rbac.require(store.role, "org.configure", store.actor)
    conn = _conn(settings)
    try:
        ok = ledger.set_destination_enabled(conn, store.org_id, destination_id,
                                            enabled)
    finally:
        conn.close()
    if not ok:
        raise RouteError(404, f"no destination {destination_id}")
    store.audit("destination.enable" if enabled else "destination.disable",
                destination_id, {})
    return {"id": destination_id, "enabled": enabled}


@route("POST", "/api/destinations/{destination_id}/disable")
def disable_destination(store: Store, settings: Settings, destination_id: str,
                        **_) -> dict:
    return _toggle(store, settings, destination_id, False)


@route("POST", "/api/destinations/{destination_id}/enable")
def enable_destination(store: Store, settings: Settings, destination_id: str,
                       **_) -> dict:
    return _toggle(store, settings, destination_id, True)


@route("POST", "/api/destinations/{destination_id}/test")
def test_destination(store: Store, settings: Settings, destination_id: str,
                     **_) -> dict:
    """Send one clearly-labelled test delivery, now, and report what happened."""
    rbac.require(store.role, "org.configure", store.actor)
    conn = _conn(settings)
    try:
        rows = {d.id: d for d in ledger.list_destinations(conn, store.org_id)}
    finally:
        conn.close()
    d = rows.get(destination_id)
    if d is None:
        raise RouteError(404, f"no destination {destination_id}")
    dispatcher = ServiceDispatcher(
        destinations=[Destination(d.id, Channel(d.channel), d.target, d.secret,
                                  4, True)],
        base_url=settings.public_base_url, smtp=settings.smtp,
        allow_private=settings.webhook_allow_private, sleep=lambda _s: None)
    attempt = dispatcher.dispatch({"id": f"test-{secrets.token_hex(4)}",
                                   "priority": 4, "severity": "test",
                                   "rule": "test"})[0]
    store.audit("destination.test", d.id, {"outcome": attempt.outcome.value})
    return {"delivered": attempt.outcome is Outcome.DELIVERED,
            "attempt": attempt.to_dict()}


@route("GET", "/api/deliveries")
def list_deliveries(store: Store, settings: Settings, params: dict, **_) -> dict:
    rbac.require(store.role, "alert.read", store.actor)
    limit = max(1, min(int(_one(params, "limit", "200") or 200), 1000))
    conn = _conn(settings)
    try:
        rows = ledger.list_deliveries(conn, store.org_id, limit)
    finally:
        conn.close()
    failed = [r for r in rows if r["outcome"] == Outcome.FAILED.value]
    return {"deliveries": rows, "failed": len(failed),
            "note": ("a failed delivery means nobody was told; it is not the "
                     "same as a quiet period")}


# -- operations ----------------------------------------------------------------

@route("GET", "/api/providers")
def provider_status(store: Store, **_) -> dict:
    """Which imagery source is selected and what each would need. No secrets."""
    rbac.require(store.role, "site.read", store.actor)
    return providers.status()


@route("GET", "/api/worker")
def worker_status(store: Store, settings: Settings, params: dict, **_) -> dict:
    """When monitoring last ran, and this organisation's recent area-days.

    The cycle summary itself is not returned: it spans every organisation.
    """
    rbac.require(store.role, "site.read", store.actor)
    limit = max(1, min(int(_one(params, "limit", "100") or 100), 500))
    conn = _conn(settings)
    try:
        state = ledger.worker_state(conn)
        runs = ledger.recent_runs(conn, store.org_id, limit)
    finally:
        conn.close()
    return {"heartbeat_at": state["heartbeat_at"],
            "interval_s": settings.worker_interval_s,
            "runs": runs,
            "errors": [r for r in runs if r["status"] in ("error", "skipped")]}
