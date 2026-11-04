"""TerraSync backend: the production service around the TerraShield engine.

The engine (`terrashield`) owns the analysis, the store, the API contract and
the web application. This package owns what it takes to run them for real:

- `app`          an ASGI server that preserves the engine's API contract
- `worker`       the scheduled monitoring run and alert delivery
- `destinations` per-organisation webhook and email destinations
- `bootstrap`    first-admin creation and demo seeding for a fresh deploy
"""

__version__ = "0.1.0"
