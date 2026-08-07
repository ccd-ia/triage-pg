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
from collections.abc import Sequence
from typing import Any, Optional

import psycopg
from psycopg import sql

from triage import registry
from triage.logging import get_logger
from triage.util.db import DictRowPool, libpq_conninfo, swap_dbname

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
    "grant_project_role",
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


def grant_project_role(
    project_url: str,
    *,
    owner: str,
    database_name: str,
    data_schemas: Sequence[str] = (),
    create_missing_data_schemas: bool = False,
) -> list[str]:
    """Give the per-project role what the pipeline actually needs — including *ownership*.

    Runs as the master (``project_url`` is the maintenance connection pointed at the project
    database), and is idempotent: re-running it is a no-op, so it is safe to re-apply after a
    migration.

    Grants are enough for every object the pipeline touches *except one*.
    ``REFRESH MATERIALIZED VIEW`` is owner-only — PostgreSQL has no grantable REFRESH
    privilege, the way it has SELECT or INSERT — so a role that merely holds every grant in the
    catalog still cannot refresh ``triage.leaderboard`` (ADR-0007). Since ``create_project``
    applies the migrations as the master, the master owns every object they created, matviews
    included. The only fix is to hand the matview over.

    We reassign **every** matview in the ``triage`` schema rather than naming ``leaderboard``,
    so a matview added by a future migration is covered without anyone remembering this
    function exists. Ownership transfer additionally requires the master to be a *member* of
    the target role (a PostgreSQL rule for ``ALTER … OWNER TO``), which is why the membership
    grant comes first.

    ``data_schemas`` extends the same grant body to the operator's own schemas (``raw``,
    ``clean``, … — whatever *they* name; triage invents no convention, ADR-0008 keeps it
    ignorant of the data model). Ownership is **not** transferred there: the matview handover
    exists only because REFRESH is owner-only on triage's own matviews, and reassigning the
    operator's data objects would be a different, unrequested act. A named schema that does
    not exist raises — unless ``create_missing_data_schemas`` is set, which ``create_project``
    uses because a freshly created database *cannot* have them yet; the standalone repair path
    (``triage project grant``) keeps fail-loud so a typo cannot silently provision an empty
    schema.

    Returns the statements executed, in order — the caller logs them so the operator can see
    exactly what was changed, and can replay them by hand if they need to.
    """
    executed: list[str] = []
    with _maint_connection(project_url) as conn:
        role = sql.Identifier(owner)
        statements: list[sql.Composed] = [
            sql.SQL("grant connect, create, temporary on database {} to {}").format(
                sql.Identifier(database_name), role
            ),
        ]
        for schema in data_schemas:
            exists = conn.execute(
                "select 1 from pg_namespace where nspname = %(schema)s",
                {"schema": schema},
            ).fetchone()
            if exists is None:
                if not create_missing_data_schemas:
                    raise ValueError(
                        f"schema {schema!r} does not exist in database"
                        f" {database_name!r} — nothing was granted. Create it (and load"
                        " your data) first, or provision it at create time with"
                        " 'triage project create … --data-schema'."
                    )
                statements.append(
                    sql.SQL("create schema {}").format(sql.Identifier(schema))
                )
        for schema in ("triage", *data_schemas):
            target = sql.Identifier(schema)
            statements += [
                sql.SQL("grant usage, create on schema {} to {}").format(target, role),
                sql.SQL(
                    "grant select, insert, update, delete on all tables in schema {} to {}"
                ).format(target, role),
                sql.SQL(
                    "grant usage, select on all sequences in schema {} to {}"
                ).format(target, role),
                # Objects created LATER (a migration; a data load) would otherwise
                # land ungranted.
                sql.SQL(
                    "alter default privileges in schema {}"
                    " grant select, insert, update, delete on tables to {}"
                ).format(target, role),
                sql.SQL(
                    "alter default privileges in schema {}"
                    " grant usage, select on sequences to {}"
                ).format(target, role),
            ]
        for statement in statements:
            conn.execute(statement)
            executed.append(statement.as_string(conn))

        # ALTER … OWNER TO requires membership in the target role. Skip when the master IS the
        # role (the local profile's single-user case) — granting a role to itself is an error.
        current_user = conn.execute("select current_user").fetchone()
        if current_user is not None and current_user[0] != owner:
            membership = sql.SQL("grant {} to current_user").format(role)
            conn.execute(membership)
            executed.append(membership.as_string(conn))

        matviews = conn.execute(
            "select schemaname, matviewname from pg_matviews where schemaname = 'triage'"
            " order by matviewname"
        ).fetchall()
        for schema_name, matview_name in matviews:
            transfer = sql.SQL("alter materialized view {}.{} owner to {}").format(
                sql.Identifier(schema_name), sql.Identifier(matview_name), role
            )
            conn.execute(transfer)
            executed.append(transfer.as_string(conn))

    logger.info(
        "granted project role %s on %s (%d statements, %d matview(s) reassigned,"
        " data schemas: %s)",
        owner,
        database_name,
        len(executed),
        len(matviews),
        ", ".join(data_schemas) or "none",
    )
    return executed


def create_project(
    registry_pool: DictRowPool,
    *,
    slug: str,
    maint_url: str,
    display_name: Optional[str] = None,
    database_name: Optional[str] = None,
    owner: Optional[str] = None,
    data_schemas: Sequence[str] = (),
) -> dict[str, Any]:
    """Create a project end-to-end: registry row → ``CREATE DATABASE`` → triage schema at head.

    Fail-loud, never adopt: an already-registered slug or an already-existing database is an
    error (a half-provisioned or foreign database must be inspected by a human, not silently
    claimed). If the schema migration fails after the database was created, the error says
    exactly what exists and how to proceed — no cleanup is attempted behind the caller's back.

    ``owner`` names a pre-existing PostgreSQL role (the cloud profile's per-project IAM login,
    ADR-0004) to be granted the database and handed ownership of the refreshed matviews. Omit
    it in the local profile, where the caller already owns everything it creates.
    ``data_schemas`` additionally *creates* each named schema in the fresh database (it cannot
    exist yet) and extends the same grant body to it, so data loaded later is readable by the
    role without the runbook's hand-grant step. Requires ``owner``.
    """
    if data_schemas and not owner:
        raise ValueError(
            "data_schemas requires owner — the schemas are granted to the project role,"
            " and without one there is nobody to grant them to"
        )
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

    if owner:
        try:
            grant_project_role(
                project_url,
                owner=owner,
                database_name=db_name,
                data_schemas=data_schemas,
                create_missing_data_schemas=True,
            )
        except Exception as exc:
            raise RuntimeError(
                f"project {slug!r}: the database {db_name!r} was created and migrated, but"
                f" granting role {owner!r} failed — the role must already exist"
                " ('create user … ; grant rds_iam to …', cloud-runbook §4.2). Fix the cause,"
                f" then re-run 'triage project grant {slug} --owner {owner}'."
            ) from exc
    return dict(project)


def drop_project(
    registry_pool: DictRowPool,
    *,
    slug: str,
    confirm: str,
    maint_url: str,
) -> dict[str, Any]:
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
