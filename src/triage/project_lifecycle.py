"""Project lifecycle — registry row + database + schema in one gesture (ADR-0002 completion).

ADR-0002 makes a *Project* one isolated PostgreSQL database plus a row in the registry control
plane; teardown is ``DROP DATABASE``. Until this module, only the row half existed —
``registry.create_project`` records a project but nothing creates/migrates its database, leaving
provisioning as folklore. ``triage project create`` closes that gap: registry row →
``CREATE DATABASE`` → triage schema (alembic head), fail-loud at every step.

Two connection roles, both from the environment (the credential hard rule — nothing here is ever
stored in the registry, ADR-0002/0004):

* **registry** — ``TRIAGE_REGISTRY_URL``, the control-plane database (same variable the
  dashboard lifespan uses).
* **maintenance** — ``TRIAGE_MAINT_URL``, a cluster connection allowed to CREATE/DROP DATABASE;
  defaults to the registry URL with its database swapped to ``postgres``. The *webapp* never
  holds this: database provisioning is deliberately CLI-only (least privilege — the write webapp
  creates registry rows and reports ``database_ready`` honestly instead).

Dropping keeps the registry row as a ``status='dropped'`` tombstone (``registry.submissions``
foreign-keys to it; control-plane history is audit data), while the database itself goes away
with ``DROP DATABASE … WITH (FORCE)`` (PostgreSQL 13+, which the registry schema already
requires).
"""

from __future__ import annotations

import os
from typing import Optional

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from triage import registry
from triage.logging import get_logger
from triage.util.db import libpq_conninfo, swap_dbname

logger = get_logger(__name__)

REGISTRY_URL_ENV = "TRIAGE_REGISTRY_URL"
MAINT_URL_ENV = "TRIAGE_MAINT_URL"

__all__ = [
    "registry_url_from_env",
    "maintenance_url",
    "database_exists",
    "database_ready",
    "create_project",
    "drop_project",
]


def registry_url_from_env() -> str:
    """The registry control-plane URL, from ``TRIAGE_REGISTRY_URL`` — fail fast when unset."""
    url = os.environ.get(REGISTRY_URL_ENV)
    if not url:
        raise ValueError(
            f"{REGISTRY_URL_ENV} is not set — the project lifecycle needs the registry"
            " control-plane database (ADR-0002). Set it in the environment (direnv/.envrc)"
            " and retry."
        )
    return url


def maintenance_url(registry_url: Optional[str] = None) -> str:
    """The cluster connection used for CREATE/DROP DATABASE.

    ``TRIAGE_MAINT_URL`` when set; otherwise the registry URL with its database segment swapped
    to ``postgres`` (the registry lives in the same cluster the projects do, ADR-0002).
    """
    explicit = os.environ.get(MAINT_URL_ENV)
    if explicit:
        return explicit
    if registry_url:
        return swap_dbname(registry_url, "postgres")
    raise ValueError(
        f"cannot resolve a maintenance connection: set {MAINT_URL_ENV} (a URL to the"
        " cluster's maintenance database, e.g. …/postgres) or set"
        f" {REGISTRY_URL_ENV} to derive it from."
    )


def _maint_connection(maint_url: str) -> psycopg.Connection:
    # CREATE/DROP DATABASE cannot run inside a transaction block → autocommit.
    return psycopg.connect(libpq_conninfo(maint_url), autocommit=True)


def _alembic_url(url: str) -> str:
    """Alembic (the one SQLAlchemy holdout, ADR-0019) wants the ``+psycopg`` driver tag."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def database_exists(maint_url: str, database_name: str) -> bool:
    with _maint_connection(maint_url) as conn:
        row = conn.execute(
            "select 1 from pg_database where datname = %(db)s", {"db": database_name}
        ).fetchone()
    return row is not None


def create_project(
    registry_pool: ConnectionPool,
    *,
    slug: str,
    maint_url: str,
    display_name: Optional[str] = None,
    database_name: Optional[str] = None,
) -> dict:
    """Create a project end-to-end: registry row → ``CREATE DATABASE`` → triage schema at head.

    Fail-loud, never adopt: an already-registered slug or an already-existing database is an
    error (a half-provisioned or foreign database must be inspected by a human, not silently
    claimed). If the schema migration fails after the database was created, the error says
    exactly what exists and how to proceed — no cleanup is attempted behind the caller's back.
    """
    existing = registry.get_project(registry_pool, slug)
    if existing is not None:
        raise ValueError(
            f"project {slug!r} is already registered (status={existing['status']!r}) —"
            " no silent adopt; pick another slug or drop it first"
        )
    db_name = database_name or slug
    if database_exists(maint_url, db_name):
        raise ValueError(
            f"database {db_name!r} already exists on the cluster — no silent adopt"
            " (ADR-0002); drop it or choose another --database-name"
        )

    project = registry.create_project(
        registry_pool,
        slug=slug,
        display_name=display_name or slug,
        database_name=db_name,
    )
    with _maint_connection(maint_url) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(db_name)))
    logger.info("created database %s for project %s", db_name, slug)

    # Deferred import: results_schema pulls in alembic/SQLAlchemy — migration-only deps that
    # shouldn't load for read-side registry use of this module.
    from triage.component.results_schema import upgrade_db

    project_url = swap_dbname(maint_url, db_name)
    try:
        upgrade_db(revision="head", dburl=_alembic_url(project_url))
    except Exception as exc:
        raise RuntimeError(
            f"project {slug!r}: the registry row and database {db_name!r} were created but"
            " applying the triage schema failed — fix the cause, then either run"
            f" 'triage db upgrade' against {db_name!r} or tear down with"
            f" 'triage project drop {slug} --confirm {slug}'."
        ) from exc
    logger.info("applied triage schema (head) to %s", db_name)
    return dict(project)


def drop_project(
    registry_pool: ConnectionPool,
    *,
    slug: str,
    confirm: str,
    maint_url: str,
) -> dict:
    """``DROP DATABASE … WITH (FORCE)`` + tombstone the registry row (``status='dropped'``).

    ``confirm`` must repeat the slug exactly — the standard guard for an irreversible teardown.
    A registry row whose database is already gone is tombstoned anyway (with a loud warning):
    tombstoning is the correct repair for that half-state.
    """
    if confirm != slug:
        raise ValueError(
            f"refusing to drop {slug!r}: --confirm must repeat the slug exactly"
            f" (got {confirm!r})"
        )
    project = registry.get_project(registry_pool, slug)
    if project is None:
        raise ValueError(f"no registry project with slug {slug!r}")
    if project["status"] == "dropped":
        raise ValueError(f"project {slug!r} is already dropped")

    db_name = project["database_name"]
    if database_exists(maint_url, db_name):
        with _maint_connection(maint_url) as conn:
            conn.execute(
                sql.SQL("drop database {} with (force)").format(sql.Identifier(db_name))
            )
        logger.info("dropped database %s (project %s)", db_name, slug)
    else:
        logger.warning(
            "project %s: database %s does not exist — tombstoning the registry row anyway",
            slug,
            db_name,
        )
    return registry.mark_project_dropped(registry_pool, slug=slug)


def database_ready(project_url: str, *, connect_timeout: int = 3) -> bool:
    """Readiness probe: can we connect AND does the triage schema exist?

    Used by the write webapp so ``POST /api/projects`` reports honestly that a freshly-registered
    project still awaits ``triage project create`` provisioning. This is a probe, not control
    flow: every failure mode (no database, no route, no schema, timeout) means the same thing —
    "not ready" — so the catch-all is deliberate; the reason is logged, never swallowed silently.
    """
    try:
        with psycopg.connect(
            libpq_conninfo(project_url), connect_timeout=connect_timeout
        ) as conn:
            row = conn.execute(
                "select to_regclass('triage.experiments') as reg"
            ).fetchone()
        return row is not None and row[0] is not None
    except Exception as exc:  # noqa: BLE001 — probe semantics, reason logged above
        logger.info("database_ready probe failed: %s", exc)
        return False
