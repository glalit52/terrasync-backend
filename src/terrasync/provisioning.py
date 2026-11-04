"""Getting a fresh deployment and a fresh organisation into a working state."""

from __future__ import annotations

import dataclasses
import logging
from datetime import date

from terrashield import alerts as alert_engine
from terrashield import pipeline, providers, sites
from terrashield.accounts import Accounts, AuthError
from terrashield.domain import Role
from terrashield.store import Store
from terrashield.store.schema import migrate

from . import ledger
from .config import Settings

log = logging.getLogger("terrasync.provisioning")


def prepare_database(db_path: str) -> None:
    """Engine migrations, then the backend's own tables. Idempotent."""
    conn = ledger.connect(db_path)
    try:
        migrate(conn)
        ledger.migrate(conn)
    finally:
        conn.close()


def has_accounts(db_path: str) -> bool:
    conn = ledger.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM users "
                            "WHERE password_hash != ''").fetchone()[0] > 0
    finally:
        conn.close()


def ensure_admin(settings: Settings) -> str | None:
    """Create the first organisation and administrator from the environment.

    The container equivalent of `terrashield register`: the person who can set
    the deployment's environment already controls the host, which is the
    property that makes bootstrapping this way safe. Does nothing if the
    address is already registered, so it is safe to run on every start.
    Returns the new organisation id, or None.
    """
    if not (settings.admin_email and settings.admin_password):
        return None
    conn = ledger.connect(settings.db_path)
    try:
        migrate(conn)
        accounts = Accounts(conn)
        if accounts.find_by_email(settings.admin_email) is not None:
            return None
        try:
            account = accounts.register_organisation(
                org_name=settings.admin_org, email=settings.admin_email,
                password=settings.admin_password)
        except AuthError as e:
            raise SystemExit(f"cannot create the first administrator: {e}") from e
    finally:
        conn.close()
    log.info("created organisation %s with administrator %s",
             account.org_id, account.email)
    with Store(settings.db_path, org_id=account.org_id, actor="terrasync-bootstrap",
               role=Role.ADMIN) as store:
        ensure_default_rules(store)
    return account.org_id


def ensure_default_rules(store: Store) -> int:
    """Install the engine's starter rules for an organisation that has none.

    Needed because the engine's sign-up path creates an organisation without
    rules, and an organisation without rules never raises an alert. The ids are
    suffixed with the organisation: the engine's defaults use fixed ids and
    `rules.id` is a global key, so installing them as-is for a second
    organisation would overwrite the first organisation's rows.

    An organisation that already has any rule -- enabled or not -- is left
    alone; that is a customer's configuration, not a gap.
    """
    if store.list_rules(enabled_only=False):
        return 0
    installed = 0
    for rule in alert_engine.default_rules(store.org_id):
        store.put_rule(dataclasses.replace(rule, id=f"{rule.id}--{store.org_id}"))
        installed += 1
    return installed


def org_for_email(db_path: str, email: str) -> str:
    conn = ledger.connect(db_path)
    try:
        account = Accounts(conn).find_by_email(email)
    finally:
        conn.close()
    if account is None:
        raise SystemExit(f"no account for {email}")
    return account.org_id


def seed_demo(db_path: str, org_id: str, since: date, to: date) -> list[dict]:
    """Enrol the engine's four demo sites into an organisation and monitor them.

    Always uses the synthetic provider: the demo sites' scripted events exist
    only in the modelled world. The site ids are global, so the demo estate can
    belong to one organisation per database; a site held by another
    organisation is skipped rather than taken.
    """
    provider = providers.resolve("synthetic")
    out = []
    conn = ledger.connect(db_path)
    try:
        owners = {r[0]: r[1] for r in conn.execute("SELECT id, org_id FROM aois")}
    finally:
        conn.close()
    with Store(db_path, org_id=org_id, actor="terrasync-seed",
               role=Role.ADMIN) as store:
        ensure_default_rules(store)
        for demo in sites.load_all():
            owner = owners.get(demo.aoi.id)
            if owner and owner != org_id:
                out.append({"aoi_id": demo.aoi.id, "skipped":
                            "belongs to another organisation"})
                continue
            aoi = dataclasses.replace(demo.aoi, org_id=org_id)
            store.put_aoi(aoi)
            results = pipeline.run_range(store, provider, aoi, since, to)
            worked = [r for r in results if r.scene_used]
            out.append({"aoi_id": aoi.id, "name": aoi.name,
                        "usable_days": len(worked),
                        "changes": sum(r.change_events for r in worked),
                        "alerts": sum(r.alerts for r in results)})
            log.info("seeded %s: %s", aoi.id, out[-1])
    return out
