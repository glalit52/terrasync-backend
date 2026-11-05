"""Settings, read from the environment once at start-up.

Everything is an environment variable because that is the one configuration
surface every deployment target -- Docker, Compose, Render, Fly, systemd --
has in common, and because secrets must never live in a file that can be
committed. Imagery provider selection keeps the engine's own variables
(`TERRASHIELD_PROVIDER`, `TERRASHIELD_<VENDOR>_*`) and is not duplicated here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

PREFIX = "TERRASYNC_"


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _int(value: str | None, default: int, *, minimum: int = 0) -> int:
    if value is None or value.strip() == "":
        return default
    n = int(value)
    if n < minimum:
        raise ValueError(f"{n} is below the minimum of {minimum}")
    return n


def _list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(p.strip() for p in value.split(",") if p.strip())


@dataclass(frozen=True)
class SmtpSettings:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""
    starttls: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


@dataclass(frozen=True)
class Settings:
    db_path: str = "terrasync.db"
    webapp_path: str = ""
    #: The externally visible origin, e.g. https://terrasync.example.com. Used
    #: for links in alert deliveries and to decide whether to send HSTS.
    public_base_url: str = ""

    allow_signup: bool = False
    #: Origins allowed to call the API cross-origin. Empty is correct when the
    #: web application is served by this process (the default).
    cors_origins: tuple[str, ...] = ()
    #: Addresses whose X-Forwarded-* headers are believed. Set to the reverse
    #: proxy's address; "*" only when nothing but the proxy can reach the port.
    forwarded_allow_ips: str = "127.0.0.1"

    max_body_bytes: int = 1_000_000
    auth_rate_limit: int = 10          # attempts ...
    auth_rate_window_s: int = 300      # ... per client per window

    #: A static bearer token for a service account, bound to one organisation.
    service_token: str = field(default="", repr=False)
    service_org: str = ""

    #: Run the monitoring worker inside the API process. Right for a single
    #: container with a single disk (Render, Fly); wrong when a separate
    #: worker service is deployed, though the lease makes it harmless.
    embedded_worker: bool = False
    #: How often to look for newly published imagery. Satellites pass every
    #: few days and providers publish hours after acquisition, so an hour
    #: keeps findings within about an hour of publication at one catalogue
    #: search per area per cycle. Lower it if that matters more than requests.
    worker_interval_s: int = 3600
    #: How far back a newly enrolled area is monitored, so it starts with a
    #: baseline rather than a single day.
    backfill_days: int = 14
    #: Days younger than this are left for a later cycle. Zero processes today
    #: as soon as imagery exists; scenes published later are caught by
    #: `recheck_days`, so nothing is processed once and missed.
    settle_days: int = 0
    #: How far back a newly published scene still triggers reprocessing of
    #: its day. Providers publish late; a scene that appears after its day
    #: was analysed would otherwise never be looked at.
    recheck_days: int = 3
    #: Upper bound on area-days per area per cycle, so one large backfill does
    #: not starve every other area.
    max_days_per_cycle: int = 7
    #: Attempts on one area-day before it is recorded as skipped and the area
    #: moves on. Without a bound, one bad day blocks the area forever.
    max_day_attempts: int = 3

    #: Allow webhook targets on private, loopback or link-local addresses.
    #: Off by default: an org admin must not be able to make this server send
    #: requests into the network it runs in. Turn on for on-premise installs
    #: whose receivers are internal.
    webhook_allow_private: bool = False
    smtp: SmtpSettings = field(default_factory=SmtpSettings)

    # -- bootstrap ---------------------------------------------------------
    admin_email: str = ""
    admin_password: str = field(default="", repr=False)
    admin_org: str = "TerraSync"

    @property
    def secure_origin(self) -> bool:
        return self.public_base_url.lower().startswith("https://")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env

        def g(name: str) -> str | None:
            return e.get(PREFIX + name)

        return cls(
            db_path=g("DB") or "terrasync.db",
            webapp_path=g("WEBAPP") or "",
            public_base_url=(g("PUBLIC_BASE_URL") or "").rstrip("/"),
            allow_signup=_bool(g("ALLOW_SIGNUP"), False),
            cors_origins=_list(g("CORS_ORIGINS")),
            forwarded_allow_ips=g("FORWARDED_ALLOW_IPS") or "127.0.0.1",
            max_body_bytes=_int(g("MAX_BODY_BYTES"), 1_000_000, minimum=1024),
            auth_rate_limit=_int(g("AUTH_RATE_LIMIT"), 10, minimum=1),
            auth_rate_window_s=_int(g("AUTH_RATE_WINDOW_S"), 300, minimum=1),
            service_token=e.get("TERRASHIELD_TOKEN", "") or g("SERVICE_TOKEN") or "",
            service_org=g("SERVICE_ORG") or "",
            embedded_worker=_bool(g("EMBEDDED_WORKER"), False),
            worker_interval_s=_int(g("WORKER_INTERVAL_S"), 3600, minimum=60),
            backfill_days=_int(g("BACKFILL_DAYS"), 14, minimum=1),
            settle_days=_int(g("SETTLE_DAYS"), 0, minimum=0),
            recheck_days=_int(g("RECHECK_DAYS"), 3, minimum=0),
            max_days_per_cycle=_int(g("MAX_DAYS_PER_CYCLE"), 7, minimum=1),
            max_day_attempts=_int(g("MAX_DAY_ATTEMPTS"), 3, minimum=1),
            webhook_allow_private=_bool(g("WEBHOOK_ALLOW_PRIVATE"), False),
            smtp=SmtpSettings(
                host=g("SMTP_HOST") or "",
                port=_int(g("SMTP_PORT"), 587, minimum=1),
                username=g("SMTP_USERNAME") or "",
                password=g("SMTP_PASSWORD") or "",
                sender=g("SMTP_FROM") or "",
                starttls=_bool(g("SMTP_STARTTLS"), True),
            ),
            admin_email=g("ADMIN_EMAIL") or "",
            admin_password=g("ADMIN_PASSWORD") or "",
            admin_org=g("ADMIN_ORG") or "TerraSync",
        )
