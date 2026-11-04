"""Alert delivery, on top of the engine's dispatcher.

The engine's `Dispatcher` owns the rules that matter -- the whitelist payload,
HMAC signing over timestamp and body, bounded retries, failures recorded
rather than swallowed. This module adds what a deployment needs around it:

- email over a configured SMTP relay (the engine deliberately refuses to fake
  this and records a failure; with a relay configured, mail is sent),
- re-validation of webhook targets before every send,
- per-organisation destinations and a durable ledger, so each alert reaches
  each destination once, and a restart neither loses nor repeats deliveries.
"""

from __future__ import annotations

import json
import logging
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Callable

from terrashield.delivery import (Attempt, Channel, DeliveryError, Destination,
                                  Dispatcher, Outcome, redact)
from terrashield.providers.transport import Transport, UrllibTransport
from terrashield.store import Store

from . import ledger
from .config import Settings, SmtpSettings
from .targets import Resolver, TargetError, check_webhook, system_resolver

log = logging.getLogger("terrasync.delivery")

#: Deliveries per destination per cycle. A new destination on an estate with a
#: backlog should not receive five hundred messages in a minute; the rest go
#: out on later cycles, most urgent first.
PER_CYCLE_CAP = 50


@dataclass
class ServiceDispatcher(Dispatcher):
    smtp: SmtpSettings = field(default_factory=SmtpSettings)
    smtp_factory: Callable[..., Any] | None = None
    allow_private: bool = False
    resolver: Resolver = system_resolver

    def _send(self, destination: Destination, alert: dict,
              alert_id: str) -> Attempt:
        if destination.channel is Channel.EMAIL:
            if self.smtp.configured:
                return self._send_email(destination, alert, alert_id)
            return super()._send(destination, alert, alert_id)
        try:
            check_webhook(destination.target, allow_private=self.allow_private,
                          resolver=self.resolver)
        except TargetError as e:
            return Attempt(destination.id, alert_id, Outcome.FAILED, self._now(),
                           attempts=0, detail=f"target refused: {e}")
        return super()._send(destination, alert, alert_id)

    def _send_email(self, destination: Destination, alert: dict,
                    alert_id: str) -> Attempt:
        #: The same redacted payload a webhook gets. Mailboxes are forwarded,
        #: archived and searched by systems this platform does not control.
        payload = redact(alert, self.base_url)
        msg = EmailMessage()
        msg["Subject"] = (f"[TerraSync] P{payload.get('priority', '?')} "
                          f"{payload.get('severity', '')} alert {alert_id}").strip()
        msg["From"] = self.smtp.sender
        msg["To"] = destination.target
        lines = ["An alert needs attention.", ""]
        if payload.get("url"):
            lines += [f"Open it: {payload['url']}", ""]
        lines += [json.dumps(payload, indent=2, sort_keys=True)]
        msg.set_content("\n".join(lines))
        try:
            factory = self.smtp_factory or (
                smtplib.SMTP_SSL if self.smtp.port == 465 else smtplib.SMTP)
            with factory(self.smtp.host, self.smtp.port, timeout=15) as s:
                if self.smtp.starttls and self.smtp.port != 465:
                    s.starttls(context=ssl.create_default_context())
                if self.smtp.username:
                    s.login(self.smtp.username, self.smtp.password)
                s.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            return Attempt(destination.id, alert_id, Outcome.FAILED, self._now(),
                           attempts=1, detail=f"SMTP: {type(e).__name__}: {e}")
        return Attempt(destination.id, alert_id, Outcome.DELIVERED, self._now(),
                       attempts=1, status=250)


def to_alert(row: dict) -> dict:
    """An alert row from the store, in the shape the engine's redact expects."""
    return {"id": row["id"], "alert_id": row["id"], "aoi_id": row.get("aoi_id", ""),
            "priority": row.get("priority", 4), "severity": row.get("severity", ""),
            "raised_at": row.get("created_at", ""), "rule": row.get("rule_id", "")}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def deliver_for_org(conn: sqlite3.Connection, store: Store, settings: Settings,
                    transport: Transport | None = None,
                    sleep: Callable[[float], None] | None = None,
                    smtp_factory: Callable[..., Any] | None = None,
                    resolver: Resolver | None = None) -> dict:
    org = store.org_id
    rows = store.list_alerts(limit=1000)
    #: Recorded whether or not anything is configured, so a destination added
    #: later starts from that moment rather than receiving the whole history.
    ledger.mark_seen(conn, org, [r["id"] for r in rows])
    destinations = ledger.list_destinations(conn, org, enabled_only=True)
    summary = {"delivered": 0, "failed": 0, "suppressed": 0, "deferred": 0}
    if not destinations:
        return summary

    seen = {k: _parse(v) for k, v in ledger.seen_at(conn, org).items()}
    done = ledger.delivered_pairs(conn, org)
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    dispatcher = ServiceDispatcher(
        transport=transport or UrllibTransport(max_attempts=1),
        base_url=settings.public_base_url, smtp=settings.smtp,
        smtp_factory=smtp_factory, allow_private=settings.webhook_allow_private,
        resolver=resolver or system_resolver, **kwargs)

    for d in destinations:
        dest = Destination(d.id, Channel(d.channel), d.target, d.secret,
                           d.min_priority, True)
        since = _parse(d.created_at)
        pending = [r for r in rows if (r["id"], d.id) not in done
                   and r["id"] in seen and seen[r["id"]] >= since]
        pending.sort(key=lambda r: (r.get("priority", 4), r.get("created_at", "")))
        summary["deferred"] += max(len(pending) - PER_CYCLE_CAP, 0)
        dispatcher.destinations = [dest]
        for row in pending[:PER_CYCLE_CAP]:
            try:
                attempts = dispatcher.dispatch(to_alert(row))
            except DeliveryError as e:
                attempts = [Attempt(d.id, row["id"], Outcome.FAILED,
                                    dispatcher._now(), attempts=0, detail=str(e))]
            for a in attempts:
                ledger.record_delivery(conn, org, a.alert_id, a.destination_id,
                                       a.outcome.value, a.attempts, a.status,
                                       a.detail, a.at)
                summary[a.outcome.value] += 1
            #: One failure means the endpoint is down or refusing. Carrying on
            #: would spend the whole backoff schedule on every remaining alert;
            #: they stay pending for the next cycle instead.
            if any(a.outcome is Outcome.FAILED for a in attempts):
                log.warning("delivery to %s failed: %s", d.id, attempts[-1].detail)
                break
    return summary
