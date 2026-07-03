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
                "select version_num from results_schema_versions"
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
