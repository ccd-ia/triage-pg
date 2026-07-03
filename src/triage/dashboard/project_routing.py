"""Per-project database routing for the dashboard (ADR-0002/0025 — the project switcher).

The read dashboard binds ONE project database per app instance. To let one instance serve many
projects (the ADR-0002 shared cluster), a request may name the active project with an
``X-Triage-Project: <slug>`` header; this module resolves that slug to a ``ConnectionPool`` on the
project's own database, opening + caching one pool per distinct database URL.

Credentials never live in the registry (ADR-0002): the registry stores each project's
``database_name``, not how to authenticate to it. This module turns ``database_name`` into a URL two
ways, in order:

1. ``TRIAGE_PROJECT_DB_MAP`` — an optional env JSON ``{slug: url}`` override (the *local* profile
   "uses env" path; needed when projects live in separate clusters/containers, as the tutorial
   dockers do). The registry table stays credential-free — the map is environment, not data.
2. **dbname swap** on the base project connection — take the app's base URL (host/port/creds) and
   replace only the database name with ``database_name``. This is the ADR-0002 shared-cluster path
   and the cloud path (one RDS endpoint + IAM, database per project).

No selection (or no registry) ⇒ the app's default bound pool, so single-project use is unchanged.
"""

from __future__ import annotations

import json
import os

from fastapi import HTTPException, Request
from psycopg_pool import ConnectionPool

from triage import registry
from triage.logging import get_logger
from triage.util.db import connection_pool, libpq_conninfo, swap_dbname

logger = get_logger(__name__)

PROJECT_HEADER = "X-Triage-Project"
_DB_MAP_ENV = "TRIAGE_PROJECT_DB_MAP"


def active_project_slug(request: Request) -> str | None:
    """The requested active project slug (``X-Triage-Project`` header), or ``None``."""
    slug = request.headers.get(PROJECT_HEADER)
    slug = slug.strip() if slug else ""
    return slug or None


def _project_db_map() -> dict[str, str]:
    raw = os.environ.get(_DB_MAP_ENV)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_DB_MAP_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"{_DB_MAP_ENV} must be a JSON object mapping slug -> database URL"
        )
    return parsed


def project_dburl(slug: str, database_name: str, base_url: str | None) -> str:
    """Resolve a project's database URL (env-map override, else dbname-swap on ``base_url``)."""
    mapping = _project_db_map()
    if slug in mapping:
        return mapping[slug]
    if base_url:
        return swap_dbname(base_url, database_name)
    raise ValueError(
        f"cannot resolve a database URL for project {slug!r}: no {_DB_MAP_ENV} entry and no base"
        " project URL to swap the database name onto (set TRIAGE_PROJECT_DB_MAP for a local"
        " multi-cluster setup, ADR-0025)"
    )


def _pool_for(app, slug: str, database_name: str) -> ConnectionPool:
    """Open (or reuse) the pool for a project's database. Raises 503 on an unroutable project.

    Reuses the app's bound (default) pool when the resolved URL is the same database, so the common
    single-cluster case never opens a duplicate; otherwise opens once and caches on
    ``app.state.project_pools``.
    """
    default_pool: ConnectionPool | None = getattr(app.state, "pool", None)
    base_url: str | None = getattr(app.state, "base_project_url", None)
    try:
        url = project_dburl(slug, database_name, base_url)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if default_pool is not None and base_url is not None:
        try:
            if libpq_conninfo(url) == libpq_conninfo(base_url):
                return default_pool
        except (TypeError, ValueError):
            pass

    cache: dict[str, ConnectionPool] = app.state.project_pools
    if url not in cache:
        logger.info("opening project pool for %r (database %s)", slug, database_name)
        cache[url] = connection_pool(url)
    return cache[url]


def resolve_active_pool(request: Request) -> ConnectionPool:
    """Resolve the pool a read request runs against — the active project, else the default.

    Falls back to the app's bound pool when: no ``X-Triage-Project`` header, no registry
    configured, or the named slug isn't a registry project (a stale selection is benign). Raises
    503 only when a *known* project can't be routed (a real config problem — never silently serve
    another project's data).
    """
    app = request.app
    default_pool: ConnectionPool | None = getattr(app.state, "pool", None)
    reg = getattr(app.state, "registry_pool", None)
    slug = active_project_slug(request)

    if slug is None or reg is None:
        if default_pool is None:
            raise RuntimeError(
                "dashboard request pool is not initialized — the app lifespan did not run."
            )
        return default_pool

    project = registry.get_project(reg, slug)
    if project is None:
        logger.warning(
            "active project %r is not a registry project; using the default pool", slug
        )
        if default_pool is None:
            raise RuntimeError("dashboard request pool is not initialized.")
        return default_pool

    return _pool_for(app, slug, project["database_name"])


def pool_for_slug(request: Request, slug: str, database_name: str) -> ConnectionPool:
    """The pool for an explicitly-named project (the submit route runs against its target DB)."""
    return _pool_for(request.app, slug, database_name)
