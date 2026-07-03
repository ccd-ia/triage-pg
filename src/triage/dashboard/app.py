"""FastAPI app factory + lifespan pool for the read dashboard (read-dashboard-spec §5).

The app opens one psycopg3 ``ConnectionPool`` over the *project* database at startup
(reusing ``cli.resolve_db_url`` -> ``util.db.connection_pool``; PG*/DATABASE_URL from the
environment per the project DB hard rule), shares it with every request handler, and closes
it at shutdown. The API is mounted under ``/api``; ``/`` serves the built SPA bundle with a
client-side-routing fallback (spec §6).

The SPA is a single-page app (React Router): a hard navigation or refresh to a *client* route
(e.g. ``/experiments/{hash}`` or ``/ontology``) hits the server with a path that is NOT a real
file. Plain ``StaticFiles`` 404s those, breaking deep links / refresh. :class:`_SpaStaticFiles`
falls back to ``index.html`` for any non-``/api`` GET that doesn't resolve to a static file, so
React Router can take over client-side — the standard SPA-on-FastAPI pattern. ``/api/*`` is
mounted first and never reaches the static layer; real assets are still served as files.

The SSE stream (``/api/runs/{id}/stream``) holds its OWN long-lived ``LISTEN run_progress``
connection, separate from the request pool — see :mod:`triage.dashboard.routes`.
"""

from __future__ import annotations

import os
import pathlib
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from triage.cli import resolve_db_url
from triage.dashboard.auth import AuthBackend, resolve_auth_backend
from triage.dashboard.oidc import auth_router
from triage.dashboard.routes import router
from triage.dashboard.write_routes import default_experiment_runner, write_router
from triage.logging import get_logger
from triage.util.db import connection_pool

logger = get_logger(__name__)

# The registry control-plane DB URL for the write surface (ADR-0002). Optional: the read
# dashboard runs without it; write routes 503 when it (and an injected registry_pool) are absent.
_REGISTRY_URL_ENV = "TRIAGE_REGISTRY_URL"

# Static dir for the built SPA bundle. Defaults to the packaged static/ dir; override with
# TRIAGE_DASHBOARD_STATIC (the Docker dashboard image + the native preview point it at the
# Vite build output, spec §6).
_STATIC_DIR = pathlib.Path(
    os.environ.get("TRIAGE_DASHBOARD_STATIC")
    or (pathlib.Path(__file__).parent / "static")
)


class _SpaStaticFiles(StaticFiles):
    """``StaticFiles`` that serves ``index.html`` as the SPA fallback (client-side routing).

    A normal ``StaticFiles`` raises 404 for a path with no matching file, which breaks a hard
    navigation / refresh to a React Router client route (``/experiments/{hash}``, ``/ontology``,
    …). We override the 404 path: when the requested file is missing AND an ``index.html`` exists
    in the static root, return it (200) so the SPA bootstraps and routes client-side. A genuinely
    missing asset path with no ``index.html`` (e.g. the packaged placeholder dir) still 404s.

    This only ever runs for paths the ``/api`` router did NOT claim — ``/api`` is mounted before
    this static layer, so API 404s keep their JSON ``{"detail": "Not Found"}`` shape untouched.
    """

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Only swallow the not-found case; re-raise everything else (e.g. 405) unchanged.
            if exc.status_code != 404:
                raise
            # NEVER fall back to index.html for the API surface. An unknown /api/* path reaches
            # this static layer only because the router had no match; it must keep the JSON 404
            # ({"detail": "Not Found"}), not be served the SPA shell. ``path`` is relative to the
            # mount root ('/'), so an /api/unknown request arrives here as 'api/unknown'.
            if path == "api" or path.startswith("api/"):
                raise
            index = pathlib.Path(self.directory) / "index.html"
            if index.is_file():
                # Serve the SPA entrypoint; React Router resolves the client route in-browser.
                return await super().get_response("index.html", scope)
            raise


def _open_project_pool() -> tuple[ConnectionPool, str]:
    """Open the request pool over the project DB; return ``(pool, base_url)``.

    Reuses the inherited resolution (``--dbfile`` / ``database.yaml`` / ``DATABASE_URL`` /
    ``PG*`` / ``.env``); fails loud with the same guidance if no config is present. Never
    hardcodes credentials (project DB hard rule). The URL is retained as the *base* the project
    switcher swaps the database name onto (ADR-0025, project_routing).
    """
    dburl = resolve_db_url(None)  # password-bearing postgresql+psycopg:// string
    logger.info("dashboard: opening project DB pool")
    return connection_pool(dburl), dburl


def _open_registry_pool() -> Optional[ConnectionPool]:
    """Open the registry control-plane pool from ``TRIAGE_REGISTRY_URL`` if set, else ``None``.

    The registry is optional: the read dashboard needs no control plane, so a missing
    ``TRIAGE_REGISTRY_URL`` is not an error — the write routes simply 503 until one is configured
    (auth._registry_pool). Set it to the registry DB and run ``just alembic-registry upgrade head``.
    """
    url = os.environ.get(_REGISTRY_URL_ENV)
    if not url:
        return None
    logger.info(
        "dashboard: opening registry control-plane pool (write surface enabled)"
    )
    return connection_pool(url)


def create_app(
    pool: Optional[ConnectionPool] = None,
    *,
    registry_pool: Optional[ConnectionPool] = None,
    auth_backend: Optional[AuthBackend] = None,
    experiment_runner=None,
) -> FastAPI:
    """Build the dashboard FastAPI app (read + write surfaces).

    When ``pool`` is provided (the tests pass the ``db_pool_greenfield`` pool) it is used as-is and
    the app does not own its lifecycle; otherwise the lifespan opens a pool over the project DB from
    the environment and closes it at shutdown.

    The write surface (ADR-0002/0024) adds three injectables, all optional:

    * ``registry_pool`` — the control-plane pool. If ``None``, the lifespan opens one from
      ``TRIAGE_REGISTRY_URL`` (else leaves it ``None`` → write routes 503). Tests inject a pool.
    * ``auth_backend`` — the user-identity seam. Defaults to :func:`resolve_auth_backend` (the
      ``TRIAGE_AUTH`` env, ``trusted`` locally).
    * ``experiment_runner`` — the submit runner. Defaults to :func:`default_experiment_runner`
      (real in-process / Batch run); tests inject a stub so no training happens.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_pool = pool is None
        owns_registry = registry_pool is None
        if pool is not None:
            app.state.pool = pool
            app.state.base_project_url = getattr(pool, "conninfo", None)
        else:
            app.state.pool, app.state.base_project_url = _open_project_pool()
        app.state.registry_pool = (
            registry_pool if registry_pool is not None else _open_registry_pool()
        )
        try:
            yield
        finally:
            if owns_pool and app.state.pool is not None:
                app.state.pool.close()
            if owns_registry and app.state.registry_pool is not None:
                app.state.registry_pool.close()
            # Close every project pool the switcher opened on demand (ADR-0025).
            for proj_pool in app.state.project_pools.values():
                proj_pool.close()
            app.state.project_pools = {}

    app = FastAPI(
        title="triage-pg dashboard API",
        version="1.1",
        summary="JSON API over the in-PG views (read) + registry write surface (ADR-0012/0024).",
        lifespan=lifespan,
    )
    # Injected dependencies are stashed eagerly so a TestClient that never enters the lifespan
    # (rare) still resolves them; the lifespan re-affirms the pools.
    if pool is not None:
        app.state.pool = pool
        app.state.base_project_url = getattr(pool, "conninfo", None)
    app.state.registry_pool = registry_pool
    # The project switcher's per-project pool cache (ADR-0025), opened on demand + closed on shutdown.
    app.state.project_pools = {}
    app.state.auth_backend = (
        auth_backend if auth_backend is not None else resolve_auth_backend()
    )
    app.state.experiment_runner = (
        experiment_runner
        if experiment_runner is not None
        else default_experiment_runner
    )

    # /api first so the SPA static fallback below never shadows the JSON API (an unknown
    # /api/* path keeps its 404 {"detail": "Not Found"} rather than being served index.html).
    app.include_router(router, prefix="/api")
    app.include_router(write_router, prefix="/api")
    # Browser auth endpoints (/auth/login|callback|logout, ADR-0028). Mounted always —
    # they 404 unless the resolved backend is OidcAuth, so trusted-mode apps are unchanged.
    app.include_router(auth_router)

    if _STATIC_DIR.is_dir():
        app.mount(
            "/", _SpaStaticFiles(directory=str(_STATIC_DIR), html=True), name="static"
        )

    return app


def get_pool(request: Request) -> ConnectionPool:
    """FastAPI dependency: the per-app request pool (set by the lifespan)."""
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise RuntimeError(
            "dashboard request pool is not initialized — the app lifespan did not run; "
            "ensure the app is started via uvicorn/TestClient (which runs lifespan)."
        )
    return pool


# Module-level app for ``uvicorn triage.dashboard.app:app`` and ``from … import app``.
app = create_app()
