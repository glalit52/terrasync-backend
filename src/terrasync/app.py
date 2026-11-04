"""The ASGI application: the engine's API contract, served for production.

The engine ships a standard-library server and says plainly that it belongs
behind "a proper ASGI stack and a TLS terminator before it faces a network".
This is that stack. It does not reimplement a single engine route: requests go
to `terrashield.api.dispatch_public` and `terrashield.api.dispatch` exactly as
the engine's own handler sends them, so the contract the web application
depends on is the engine's, unchanged. What is added is what a network-facing
service needs:

- uvicorn instead of `http.server`, with proxy headers from a trusted proxy
- throttling on sign-in and sign-up, which the engine leaves to the edge
- request size limits, request ids, security headers, optional CORS
- 500s that log the traceback and return an id, instead of returning it
- liveness and readiness probes
- the web application served from a configured path, so it works from an
  installed package and not only a source checkout
- the extension routes in `routes.py`, and the embedded worker if enabled
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from terrashield.accounts import Accounts
from terrashield.api import dispatch, dispatch_public
from terrashield.domain import Role
from terrashield.rbac import AccessDenied, TenantViolation
from terrashield.store import Store

from . import ledger, routes, worker
from .config import Settings
from .provisioning import ensure_admin, has_accounts, prepare_database
from .ratelimit import SlidingWindowLimiter

log = logging.getLogger("terrasync.app")
access = logging.getLogger("terrasync.access")

#: Unauthenticated routes that are throttled per client address.
THROTTLED = frozenset({"/api/auth/login", "/api/auth/signup"})

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

REFUSAL = (
    "refusing to serve without authentication: the database has no accounts "
    "and no service token is configured. Set TERRASYNC_ADMIN_EMAIL and "
    "TERRASYNC_ADMIN_PASSWORD to create the first administrator on start, or "
    "run `terrasync bootstrap`. Open registration is not a substitute: a server "
    "with no accounts and open sign-up is one anyone who reaches it can enrol on.")


def _json(status: int, payload: Any, headers: dict | None = None) -> Response:
    return Response(json.dumps(payload, default=str), status_code=status,
                    media_type="application/json", headers=headers)


class BodyTooLarge(Exception):
    pass


class ServiceMiddleware:
    """Request id, size limit, security headers and an access log line."""

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["request_id"] = rid
        limit = self.settings.max_body_bytes
        started = time.perf_counter()
        status = {"code": 0}

        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge()
            return message

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                h = MutableHeaders(scope=message)
                h["X-Request-ID"] = rid
                h["X-Content-Type-Options"] = "nosniff"
                h["Referrer-Policy"] = "same-origin"
                h["X-Frame-Options"] = "DENY"
                if self.settings.secure_origin:
                    h["Strict-Transport-Security"] = "max-age=31536000"
                if scope["path"].startswith("/api/"):
                    h["Cache-Control"] = "no-store"
            await send(message)

        declared = dict(scope.get("headers") or []).get(b"content-length")
        try:
            refused = declared is not None and int(declared) > limit
        except ValueError:
            refused = True
        try:
            if refused:
                raise BodyTooLarge()
            await self.app(scope, limited_receive, send_with_headers)
        except BodyTooLarge:
            if not status["code"]:
                await _json(413, {"error": "request body too large",
                                  "limit_bytes": limit})(
                    scope, receive, send_with_headers)
        finally:
            access.info("%s %s %s %.1fms rid=%s", scope["method"], scope["path"],
                        status["code"], (time.perf_counter() - started) * 1000, rid)


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    limiter = SlidingWindowLimiter(settings.auth_rate_limit,
                                   settings.auth_rate_window_s)
    tokens: dict[str, tuple[str, str, Role]] = {}
    if settings.service_token and settings.service_org:
        tokens[settings.service_token] = (settings.service_org, "api-token",
                                          Role.ANALYST)

    # -- identity --------------------------------------------------------

    def open_store(token: str) -> Store:
        entry = tokens.get(token) if token else None
        if entry is not None:
            org, actor, role = entry
            return Store(settings.db_path, org_id=org, actor=actor, role=role)
        account = None
        if token:
            conn = ledger.connect(settings.db_path)
            try:
                account = Accounts(conn).resolve_session(token)
            finally:
                conn.close()
        if account is None:
            raise PermissionError("a valid bearer token is required")
        return Store(settings.db_path, org_id=account.org_id, actor=account.email,
                     role=account.role)

    def handle(method: str, path: str, params: dict, body: dict,
               token: str) -> tuple[int, dict]:
        conn = ledger.connect(settings.db_path)
        try:
            status, payload = dispatch_public(Accounts(conn), method, path, params,
                                              body, token, settings.allow_signup)
        finally:
            conn.close()
        if status:
            return status, payload

        try:
            store = open_store(token)
        except PermissionError as e:
            return 401, {"error": str(e)}
        try:
            found = routes.match(method, path)
            if found is None:
                return dispatch(store, method, path, params, body)
            fn, groups = found
            try:
                return 200, fn(store=store, settings=settings, params=params,
                               body=body, **groups)
            except routes.RouteError as e:
                return e.status, {"error": e.message}
            except (AccessDenied, TenantViolation) as e:
                return 403, {"error": str(e)}
            except Exception as e:                            # noqa: BLE001
                import traceback
                return 500, {"error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc(limit=4)}
        finally:
            store.close()

    # -- endpoints -------------------------------------------------------

    async def api(request: Request) -> Response:
        method = request.method
        rid = request.state.request_id
        if method not in ("GET", "POST"):
            return _json(405, {"error": f"{method} is not supported"},
                         {"Allow": "GET, POST"})
        path = request.url.path

        throttled = method == "POST" and path in THROTTLED
        client = request.client.host if request.client else "unknown"
        if throttled:
            wait = limiter.check(client)
            if wait:
                return _json(429, {"error": "too many attempts; try again later"},
                             {"Retry-After": str(int(wait) + 1)})

        body: Any = {}
        if method == "POST":
            raw = await request.body()
            if raw.strip():
                try:
                    body = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return _json(400, {"error": "request body is not valid JSON"})
                if not isinstance(body, dict):
                    return _json(400, {"error": "request body must be a JSON object"})

        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        params = parse_qs(request.url.query)

        status, payload = await run_in_threadpool(handle, method, path, params,
                                                  body, token)
        if throttled and status >= 400:
            limiter.record(client)
        if status >= 500:
            #: The engine puts a traceback in its 500 bodies, which is useful
            #: on a laptop and a disclosure on a network. Keep it in the log.
            log.error("rid=%s %s %s -> %s\n%s", rid, method, path,
                      payload.get("error"), payload.get("trace", ""))
            payload = {"error": "internal error", "request_id": rid}
        return _json(status, payload)

    async def webapp(request: Request) -> Response:
        page_path = settings.webapp_path
        try:
            page = await run_in_threadpool(Path(page_path).read_bytes) \
                if page_path else None
        except OSError:
            page = None
        if page is None:
            return _json(404, {"error": "web application not installed",
                               "hint": "set TERRASYNC_WEBAPP to the path of "
                                       "TerraSync's webapp/index.html"})
        return Response(page, media_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": CSP,
                                 "Cache-Control": "no-cache"})

    async def healthz(request: Request) -> Response:
        return _json(200, {"status": "ok"})

    def _ready() -> tuple[int, dict]:
        try:
            conn = ledger.connect(settings.db_path)
            try:
                conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
                state = ledger.worker_state(conn)
            finally:
                conn.close()
        except Exception as e:                                # noqa: BLE001
            log.error("readiness: %s", e)
            return 503, {"status": "unavailable", "database": "error"}
        return 200, {"status": "ok", "database": "ok",
                     "worker_heartbeat_at": state["heartbeat_at"],
                     "embedded_worker": settings.embedded_worker,
                     "signup_open": settings.allow_signup}

    async def readyz(request: Request) -> Response:
        status, payload = await run_in_threadpool(_ready)
        return _json(status, payload)

    # -- lifecycle -------------------------------------------------------

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(prepare_database, settings.db_path)
        await run_in_threadpool(ensure_admin, settings)
        if not tokens and not await run_in_threadpool(has_accounts,
                                                      settings.db_path):
            raise RuntimeError(REFUSAL)
        if not settings.webapp_path:
            log.warning("TERRASYNC_WEBAPP is not set; the API is up but / will 404")
        stop = threading.Event()
        thread = None
        if settings.embedded_worker:
            thread = threading.Thread(target=worker.run_forever,
                                      args=(settings, stop), daemon=True,
                                      name="terrasync-worker")
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=10)

    middleware = [Middleware(ServiceMiddleware, settings=settings)]
    if settings.cors_origins:
        middleware.append(Middleware(
            CORSMiddleware, allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"], max_age=600))

    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Route("/", webapp),
            Route("/index.html", webapp),
            Route("/app", webapp),
            Route("/api/{path:path}", api,
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )
    app.state.settings = settings
    return app
