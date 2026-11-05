# TerraSync backend

The production service for the TerraShield engine in
[`glalit52/TerraSync`](https://github.com/glalit52/TerraSync). It runs the
engine's API and web application on a real ASGI server, runs monitoring on a
schedule, delivers alerts, and ships deploy configuration.

The engine owns the analysis, the store, the API contract (28 routes) and the
web application. **This repository does not reimplement any of them.** It
depends on the engine at a pinned commit and routes every request through
`terrashield.api.dispatch_public` / `dispatch`, exactly as the engine's own
server does. So the contract the web app depends on stays the engine's.

```
             ┌────────────── terrasync serve (uvicorn) ───────────────┐
 browser ──► │ ServiceMiddleware: request id, size limit, sec headers │
  (TLS by    │ /            web app (engine's webapp/index.html)      │
   Caddy)    │ /healthz /readyz                                        │
             │ /api/auth/login|signup  ── throttled ──► engine public  │
             │ /api/destinations|deliveries|providers|worker ► routes.py│
             │ /api/* everything else ──────────────────► engine API  │
             └──────────────────────────┬──────────────────────────────┘
                                        │ SQLite (WAL), one volume
             ┌──────── terrasync worker ┴──────────────────────────────┐
             │ every org → every active area → each settled day:       │
             │   terrashield.pipeline.run_day(provider = env-selected) │
             │ then deliver new alerts → signed webhooks / SMTP email  │
             └─────────────────────────────────────────────────────────┘
```

## What this adds to the engine

| | Why |
|---|---|
| **uvicorn + Starlette** in place of `http.server` | The engine's own docstring: put it behind "a proper ASGI stack and a TLS terminator before it faces a network". |
| **Throttled sign-in / sign-up** (failed attempts per client address) | The engine has no lockout and says it "belongs at the edge". |
| **500s scrubbed** — traceback logged with a request id, not returned | The engine returns a traceback in 500 bodies. |
| **Body size limit, request ids, security headers, optional CORS** | Network-facing basics. |
| **Web app served from a configured path** | The engine finds `webapp/index.html` relative to its source tree, which does not exist in an installed package. The Docker image fetches it from the same pinned commit. |
| **Imagery processed as it is published** | Nothing in the engine runs the pipeline on a schedule. The worker looks for new scenes every `TERRASYNC_WORKER_INTERVAL_S` (an hour by default), analyses today's imagery the day it appears, and re-runs any recent day a scene arrives for late (`TERRASYNC_RECHECK_DAYS`), so late publication never means a missed pass. Days run oldest first, a failed day is retried then skipped, and a lease keeps two workers from double-spending imagery. Alerts go out in the same cycle. |
| **Per-organisation alert destinations** (`/api/destinations`) | The engine's `Dispatcher` signs and redacts, but nothing configured destinations or decided what had been sent. Each alert reaches each destination once; a new destination does not receive history; a failing endpoint is retried on later cycles and its failures are listed at `/api/deliveries`. |
| **Email over SMTP** | The engine refuses to fake email without a relay. With `TERRASYNC_SMTP_*` set, mail is sent, carrying the same redacted payload as webhooks. |
| **SSRF guard on webhooks** | A webhook URL is an instruction to this server to make a request. Targets must be HTTPS and resolve to public addresses, checked at creation *and* before each send. |
| **Default alert rules for every organisation** | Organisations created through sign-up get no rules in the engine, so they never raise an alert. The worker and bootstrap install the engine's defaults with organisation-scoped ids (the engine's ids are global, so installing them verbatim for a second organisation would overwrite the first's). |
| **Bootstrap from environment** | The container equivalent of `terrashield register`; the server still refuses to start with no way to authenticate. |

### Added API routes

All require a bearer token from `/api/auth/login`, and are scoped to the
caller's organisation exactly like the engine's routes.

| Route | Role | |
|---|---|---|
| `GET /api/destinations` | admin | Configured destinations (never the secret) |
| `POST /api/destinations` | admin | `{channel: "webhook"\|"email", target, min_priority: 1-4}`. Returns the webhook signing secret **once** |
| `POST /api/destinations/{id}/disable` · `/enable` | admin | |
| `POST /api/destinations/{id}/test` | admin | Sends one test delivery now and reports the outcome |
| `GET /api/deliveries` | viewer | Delivery log, failures counted |
| `GET /api/providers` | viewer | Imagery provider status — selected, ready, missing variables. No secrets |
| `GET /api/worker` | viewer | Last heartbeat, recent area-days, and **latency**: per scene and as medians, hours from acquisition to publication, to being seen, and to findings |

Unauthenticated: `GET /healthz` (liveness), `GET /readyz` (database reachable).

Webhook receivers verify each delivery's `X-TerraShield-Signature` header with
`terrashield.delivery.verify(body, header, secret)`.

## Deploy

### Docker Compose with automatic TLS (any VM)

```bash
cp .env.example .env
```

Set `TERRASYNC_DOMAIN`, `TERRASYNC_PUBLIC_BASE_URL`, `TERRASYNC_ADMIN_EMAIL` and
`TERRASYNC_ADMIN_PASSWORD` in `.env`, point the domain's DNS at the host, then:

```bash
docker compose up -d --build
```

Caddy obtains the certificate; the API serves the web app at
`https://<domain>/`; the worker runs every `TERRASYNC_WORKER_INTERVAL_S`.

### Render

`render.yaml` is a blueprint: one Docker web service with a persistent disk and
the worker embedded in the API process (a Render disk attaches to one service).
Create it from the repository in the Render dashboard and fill in the
`sync: false` variables.

### Anything else that runs a container

One image, three roles:

```bash
docker run -v ts-data:/data -p 8787:8787 --env-file .env terrasync serve
```

```bash
docker run -v ts-data:/data --env-file .env terrasync worker
```

Run exactly one `serve` process per database: SQLite takes one writer, and the
sign-in throttle is in memory. Scaling out means PostgreSQL first — see the
engine's `docs/04-deploying-for-real.md`.

### Seeding the demo estate

For a demonstration, enrol the engine's four modelled sites into the admin's
organisation and monitor their six-week scripted window on synthetic imagery
(CPU-bound; expect it to take a while):

```bash
docker compose run --rm worker seed-demo
```

## Configuration

All environment variables; `.env.example` is the annotated list. The ones that
matter first:

| Variable | Default | |
|---|---|---|
| `TERRASYNC_DB` | `/data/terrasync.db` in the image | |
| `TERRASYNC_PUBLIC_BASE_URL` | — | Links in deliveries; enables HSTS when https |
| `TERRASYNC_ADMIN_EMAIL` / `_PASSWORD` / `_ORG` | — | First administrator, created on start if new |
| `TERRASYNC_ALLOW_SIGNUP` | `0` | Self-service organisations |
| `TERRASHIELD_PROVIDER` | engine default (`earth-search`) | Imagery source; vendor keys are `TERRASHIELD_<VENDOR>_*` |
| `TERRASYNC_EMBEDDED_WORKER` | `0` | Run the worker inside `serve` |
| `TERRASYNC_WORKER_INTERVAL_S` | `3600` | How often to look for newly published imagery |
| `TERRASYNC_BACKFILL_DAYS` | `14` | History monitored for a newly enrolled area |
| `TERRASYNC_SETTLE_DAYS` | `0` | Days to hold back before first processing a day |
| `TERRASYNC_RECHECK_DAYS` | `3` | How late a scene can be published and still get its day re-analysed |
| `TERRASYNC_SMTP_HOST` / `_FROM` / … | — | Email delivery |
| `TERRASYNC_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-For` is believed |

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
```

Run locally against the engine's web app:

```bash
export TERRASYNC_WEBAPP=../TerraSync/webapp/index.html
export TERRASYNC_ADMIN_EMAIL=you@example.com TERRASYNC_ADMIN_PASSWORD='a long passphrase'
terrasync serve --host 127.0.0.1
```

## Upgrading the engine

Change the commit in `pyproject.toml` **and** `TERRASHIELD_REF` in the
`Dockerfile`; `tests/test_packaging.py` fails if they differ, because the image
would otherwise serve one commit's web app against another's API.

## How "real time" this is

Findings exist within about one worker interval of a scene being **published**.
Two things sit outside the software and set the floor:

- **Revisit.** A satellite sees a place every few days (Sentinel-2 about every
  5, both satellites combined 2-3; Sentinel-1 6-12). Nothing is known between
  passes, and an object cannot be tracked from one pass to the next.
- **Publication.** Free providers publish hours after acquisition, sometimes
  more than a day. `/api/worker` reports the measured delay for your sites
  rather than a promise; the `live-imagery` workflow prints it too.

Daily or on-demand coverage is a commercial-imagery question (Planet daily,
tasked Maxar or Umbra), wired in as providers but not yet verified.

## Known limits

- **Real imagery works; its findings are uncalibrated.** Sentinel-2 on
  `earth-search`, and Sentinel-2, Landsat and Sentinel-1 RTC on
  `planetary-computer`, run end to end: the `live-imagery` workflow monitored
  Mundra and Bhadla over ten real days with every day processed. The engine's
  detection thresholds are still the ones tuned on modelled imagery, so treat
  real-imagery alert counts as unmeasured until they are scored against
  labelled scenes. Run `live-imagery` after changing the engine pin or the
  provider; failed area-days are listed at `/api/worker`.
- **SSO / MFA** are not built (engine scope). Put the deployment behind an
  identity-aware proxy if you need them now.
- **DNS rebinding** can, in principle, slip a private address past the webhook
  check between validation and connection. On a host with sensitive internal
  services, also block egress to private ranges at the network layer.
