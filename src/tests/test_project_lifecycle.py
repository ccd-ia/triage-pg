"""Project lifecycle tests — registry row + CREATE DATABASE + schema (ADR-0002 completion).

Runs against the throwaway pytest-postgresql cluster: the function-scoped test DB doubles as
the *registry* database (``upgrade_registry_db``) AND as the maintenance connection for
CREATE/DROP DATABASE (any database on the cluster can issue those). Databases the lifecycle
creates are cluster-global, so every test that provisions one also drops it (the create→drop
round trip is itself the thing under test).
"""

from __future__ import annotations

import psycopg
import pytest

from triage import project_lifecycle, registry
from triage.component.registry_schema import upgrade_registry_db
from triage.dashboard.project_routing import project_dburl
from triage.util.db import libpq_conninfo, swap_dbname

SLUG = "lifecycle_demo"


@pytest.fixture
def registry_db(db_url, db_pool):
    """The test DB with the registry schema applied; yields (db_url, pool)."""
    upgrade_registry_db(db_url)
    return db_url, db_pool


def test_create_and_drop_project_end_to_end(registry_db, monkeypatch):
    db_url, pool = registry_db
    monkeypatch.delenv("TRIAGE_PROJECT_DB_MAP", raising=False)

    project = project_lifecycle.create_project(pool, slug=SLUG, maint_url=db_url)
    try:
        # registry row
        row = registry.get_project(pool, SLUG)
        assert row is not None and row["status"] == "active"
        assert row["database_name"] == SLUG == project["database_name"]

        # the database exists and carries the triage schema at head
        assert project_lifecycle.database_exists(db_url, SLUG)
        project_url = swap_dbname(db_url, SLUG)
        assert project_lifecycle.database_ready(project_url) is True
        with psycopg.connect(libpq_conninfo(project_url)) as conn:
            version = conn.execute(
                "select version_num from triage.results_schema_versions"
            ).fetchone()
        assert version is not None

        # the dashboard routing resolves it via the dbname-swap path (ADR-0025)
        assert project_dburl(SLUG, SLUG, db_url) == project_url
    finally:
        dropped = project_lifecycle.drop_project(
            pool, slug=SLUG, confirm=SLUG, maint_url=db_url
        )

    assert not project_lifecycle.database_exists(db_url, SLUG)
    assert dropped["status"] == "dropped" and dropped["dropped_at"] is not None
    # dropped projects leave the active listing but stay as audit tombstones
    active = [p["slug"] for p in registry.list_projects(pool)]
    assert SLUG not in active
    everything = [
        p["slug"] for p in registry.list_projects(pool, include_archived=True)
    ]
    assert SLUG in everything


def test_create_refuses_existing_slug_and_database(registry_db):
    db_url, pool = registry_db
    project_lifecycle.create_project(pool, slug=SLUG, maint_url=db_url)
    try:
        # same slug → refused before any provisioning
        with pytest.raises(ValueError, match="already registered"):
            project_lifecycle.create_project(pool, slug=SLUG, maint_url=db_url)
        # different slug, same target database → refused (no silent adopt)
        with pytest.raises(ValueError, match="already exists on the cluster"):
            project_lifecycle.create_project(
                pool, slug="other_slug", maint_url=db_url, database_name=SLUG
            )
    finally:
        project_lifecycle.drop_project(pool, slug=SLUG, confirm=SLUG, maint_url=db_url)


def test_drop_requires_exact_confirm(registry_db):
    db_url, pool = registry_db
    with pytest.raises(ValueError, match="--confirm must repeat the slug"):
        project_lifecycle.drop_project(
            pool, slug=SLUG, confirm="nope", maint_url=db_url
        )


def test_drop_unknown_slug_fails_loud(registry_db):
    db_url, pool = registry_db
    with pytest.raises(ValueError, match="no registry project"):
        project_lifecycle.drop_project(
            pool, slug="ghost", confirm="ghost", maint_url=db_url
        )


def test_env_resolution_fails_fast_naming_the_variable(monkeypatch):
    monkeypatch.delenv("TRIAGE_REGISTRY_URL", raising=False)
    monkeypatch.delenv("TRIAGE_MAINT_URL", raising=False)
    with pytest.raises(ValueError, match="TRIAGE_REGISTRY_URL"):
        project_lifecycle.registry_url_from_env()
    with pytest.raises(ValueError, match="TRIAGE_MAINT_URL"):
        project_lifecycle.maintenance_url(None)
    # derived from the registry URL when no explicit maintenance URL is set
    assert (
        project_lifecycle.maintenance_url("postgresql://u@h:5/reg")
        == "postgresql://u@h:5/postgres"
    )
    monkeypatch.setenv("TRIAGE_MAINT_URL", "postgresql://m@h:5/maintdb")
    assert (
        project_lifecycle.maintenance_url("postgresql://u@h:5/reg")
        == "postgresql://m@h:5/maintdb"
    )


def test_database_ready_probe_is_false_not_raise(db_url):
    # a database that does not exist
    assert (
        project_lifecycle.database_ready(swap_dbname(db_url, "no_such_database"))
        is False
    )
    # a database that exists but has no triage schema (the bare test DB itself)
    assert project_lifecycle.database_ready(db_url) is False


OWNER_SLUG = "lifecycle_owned"
OWNER_ROLE = "triage_lifecycle_owned"


@pytest.fixture
def project_role(db_url):
    """A password-less login role standing in for the cloud profile's per-project IAM user.

    Cluster-global like the databases, so it is dropped on the way out.
    """
    with psycopg.connect(libpq_conninfo(db_url), autocommit=True) as conn:
        conn.execute(f'create role "{OWNER_ROLE}" with login')
    yield OWNER_ROLE
    with psycopg.connect(libpq_conninfo(db_url), autocommit=True) as conn:
        conn.execute(f'drop role if exists "{OWNER_ROLE}"')


def test_create_with_owner_hands_over_matview_ownership(registry_db, project_role):
    """The §4.4 fold-in: --owner makes the project role own every triage matview.

    REFRESH MATERIALIZED VIEW is owner-only in PostgreSQL (there is no grantable REFRESH
    privilege), so ownership — not a grant — is what makes the pipeline's post-run refresh
    work under the cloud profile's per-project role (ADR-0004).
    """
    db_url, pool = registry_db
    project_lifecycle.create_project(
        pool, slug=OWNER_SLUG, maint_url=db_url, owner=project_role
    )
    try:
        project_url = swap_dbname(db_url, OWNER_SLUG)
        with psycopg.connect(libpq_conninfo(project_url)) as conn:
            matviews = conn.execute(
                "select matviewname, matviewowner from pg_matviews"
                " where schemaname = 'triage'"
            ).fetchall()
            granted = conn.execute(
                "select has_table_privilege(%s, 'triage.experiments', 'insert')",
                (project_role,),
            ).fetchone()

        # the schema really does ship a matview — otherwise this test proves nothing
        assert matviews, "expected at least one matview in the triage schema"
        assert {name for name, _ in matviews} >= {"leaderboard"}
        # ...and every one of them belongs to the project role, not the creating master
        assert all(owner == project_role for _, owner in matviews), matviews
        # grants landed too (tables are grantable; the matview is the one that is not)
        assert granted is not None and granted[0] is True
    finally:
        project_lifecycle.drop_project(
            pool, slug=OWNER_SLUG, confirm=OWNER_SLUG, maint_url=db_url
        )


def test_grant_project_role_is_idempotent(registry_db, project_role):
    """Re-applying is a no-op — it is the documented repair after a migration resets ownership."""
    db_url, pool = registry_db
    project_lifecycle.create_project(
        pool, slug=OWNER_SLUG, maint_url=db_url, owner=project_role
    )
    try:
        project_url = swap_dbname(db_url, OWNER_SLUG)
        first = project_lifecycle.grant_project_role(
            project_url, owner=project_role, database_name=OWNER_SLUG
        )
        second = project_lifecycle.grant_project_role(
            project_url, owner=project_role, database_name=OWNER_SLUG
        )
        assert first == second
        assert any("alter materialized view" in s for s in first), first
    finally:
        project_lifecycle.drop_project(
            pool, slug=OWNER_SLUG, confirm=OWNER_SLUG, maint_url=db_url
        )


def test_create_without_owner_leaves_matviews_with_the_creator(registry_db):
    """The local profile is untouched: no --owner, no grants, no ownership churn."""
    db_url, pool = registry_db
    project_lifecycle.create_project(pool, slug=OWNER_SLUG, maint_url=db_url)
    try:
        project_url = swap_dbname(db_url, OWNER_SLUG)
        with psycopg.connect(libpq_conninfo(project_url)) as conn:
            owners = conn.execute(
                "select distinct matviewowner from pg_matviews where schemaname = 'triage'"
            ).fetchall()
            current = conn.execute("select current_user").fetchone()
        assert current is not None
        assert [row[0] for row in owners] == [current[0]]
    finally:
        project_lifecycle.drop_project(
            pool, slug=OWNER_SLUG, confirm=OWNER_SLUG, maint_url=db_url
        )


DATA_SCHEMAS = ("raw", "clean")


def test_create_with_data_schemas_provisions_and_grants(registry_db, project_role):
    """--data-schema on create: the schemas are created in the fresh DB and granted.

    The catalog is the assertion target — ``has_schema_privilege`` — not the returned
    statement strings; generating SQL proves nothing about what PostgreSQL did.
    """
    db_url, pool = registry_db
    project_lifecycle.create_project(
        pool,
        slug=OWNER_SLUG,
        maint_url=db_url,
        owner=project_role,
        data_schemas=DATA_SCHEMAS,
    )
    try:
        project_url = swap_dbname(db_url, OWNER_SLUG)
        with psycopg.connect(libpq_conninfo(project_url)) as conn:
            for schema in DATA_SCHEMAS:
                for privilege in ("USAGE", "CREATE"):
                    row = conn.execute(
                        "select has_schema_privilege(%s, %s, %s)",
                        (project_role, schema, privilege),
                    ).fetchone()
                    assert row is not None and row[0] is True, (schema, privilege)
    finally:
        project_lifecycle.drop_project(
            pool, slug=OWNER_SLUG, confirm=OWNER_SLUG, maint_url=db_url
        )


def test_data_schema_default_privileges_cover_later_tables(registry_db, project_role):
    """A table created AFTER the grant is readable by the role.

    This is the ``alter default privileges`` half — the half that silently rots: the
    initial grants look complete, then the first data load creates tables the role
    cannot read.
    """
    db_url, pool = registry_db
    project_lifecycle.create_project(
        pool,
        slug=OWNER_SLUG,
        maint_url=db_url,
        owner=project_role,
        data_schemas=("raw",),
    )
    try:
        project_url = swap_dbname(db_url, OWNER_SLUG)
        with psycopg.connect(libpq_conninfo(project_url), autocommit=True) as conn:
            conn.execute("create table raw.events (id int)")
            readable = conn.execute(
                "select has_table_privilege(%s, 'raw.events', 'SELECT')",
                (project_role,),
            ).fetchone()
            writable = conn.execute(
                "select has_table_privilege(%s, 'raw.events', 'INSERT')",
                (project_role,),
            ).fetchone()
        assert readable is not None and readable[0] is True
        assert writable is not None and writable[0] is True
    finally:
        project_lifecycle.drop_project(
            pool, slug=OWNER_SLUG, confirm=OWNER_SLUG, maint_url=db_url
        )


def test_grant_missing_data_schema_fails_loud(registry_db, project_role):
    """The repair path refuses a schema that does not exist, naming it — a typo must
    not silently provision an empty schema (only ``create_project`` may create, since
    a fresh database cannot have the schemas yet)."""
    db_url, pool = registry_db
    with pytest.raises(ValueError, match="data_schemas requires owner"):
        project_lifecycle.create_project(
            pool, slug=OWNER_SLUG, maint_url=db_url, data_schemas=("raw",)
        )
    project_lifecycle.create_project(
        pool, slug=OWNER_SLUG, maint_url=db_url, owner=project_role
    )
    try:
        project_url = swap_dbname(db_url, OWNER_SLUG)
        with pytest.raises(ValueError, match=r"'nope' does not exist in database"):
            project_lifecycle.grant_project_role(
                project_url,
                owner=project_role,
                database_name=OWNER_SLUG,
                data_schemas=("nope",),
            )
    finally:
        project_lifecycle.drop_project(
            pool, slug=OWNER_SLUG, confirm=OWNER_SLUG, maint_url=db_url
        )


def test_grant_data_schemas_idempotent(registry_db, project_role):
    """Re-applying with the same --data-schema flags is a clean no-op (the documented
    repair-after-migration contract extends to the data schemas)."""
    db_url, pool = registry_db
    project_lifecycle.create_project(
        pool,
        slug=OWNER_SLUG,
        maint_url=db_url,
        owner=project_role,
        data_schemas=DATA_SCHEMAS,
    )
    try:
        project_url = swap_dbname(db_url, OWNER_SLUG)
        first = project_lifecycle.grant_project_role(
            project_url,
            owner=project_role,
            database_name=OWNER_SLUG,
            data_schemas=DATA_SCHEMAS,
        )
        second = project_lifecycle.grant_project_role(
            project_url,
            owner=project_role,
            database_name=OWNER_SLUG,
            data_schemas=DATA_SCHEMAS,
        )
        assert first == second
        assert any('on schema "raw"' in s for s in first), first
    finally:
        project_lifecycle.drop_project(
            pool, slug=OWNER_SLUG, confirm=OWNER_SLUG, maint_url=db_url
        )


def test_data_schema_matviews_not_reassigned(registry_db, project_role):
    """The negative assertion that keeps the blast radius honest: ownership transfer
    exists only because REFRESH is owner-only on triage's OWN matviews. A matview in
    the operator's data schema is their object — granting must not reassign it."""
    db_url, pool = registry_db
    project_lifecycle.create_project(
        pool,
        slug=OWNER_SLUG,
        maint_url=db_url,
        owner=project_role,
        data_schemas=("raw",),
    )
    try:
        project_url = swap_dbname(db_url, OWNER_SLUG)
        with psycopg.connect(libpq_conninfo(project_url), autocommit=True) as conn:
            conn.execute("create materialized view raw.mv as select 1 as x")
        project_lifecycle.grant_project_role(
            project_url,
            owner=project_role,
            database_name=OWNER_SLUG,
            data_schemas=("raw",),
        )
        with psycopg.connect(libpq_conninfo(project_url)) as conn:
            row = conn.execute(
                "select matviewowner from pg_matviews"
                " where schemaname = 'raw' and matviewname = 'mv'"
            ).fetchone()
            current = conn.execute("select current_user").fetchone()
        assert row is not None and current is not None
        assert row[0] == current[0] != project_role
    finally:
        project_lifecycle.drop_project(
            pool, slug=OWNER_SLUG, confirm=OWNER_SLUG, maint_url=db_url
        )
