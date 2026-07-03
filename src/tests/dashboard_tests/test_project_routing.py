"""Per-project DB routing tests — the project switcher seam (ADR-0025).

Unit-tests the URL resolution (dbname-swap + env-map override) and integration-tests that a read
request routes by the ``X-Triage-Project`` header (falling back to the bound pool for an unknown
slug / no header). Cross-database reuse is proven by pointing the env-map at the same throwaway
test DB (real cross-cluster routing is exercised live against the tutorial DBs, not in CI).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from triage import registry
from triage.component.registry_schema import upgrade_registry_db
from triage.dashboard.app import create_app
from triage.dashboard.auth import TrustedHeaderAuth
from triage.dashboard.project_routing import project_dburl
from triage.util.db import swap_dbname as _swap_dbname

# --------------------------------------------------------------------------- pure unit (no DB)


def test_swap_dbname_replaces_only_the_database():
    assert (
        _swap_dbname("postgresql+psycopg://u:p@h:5432/old?sslmode=require", "new")
        == "postgresql+psycopg://u:p@h:5432/new?sslmode=require"
    )


def test_project_dburl_env_map_overrides(monkeypatch):
    monkeypatch.setenv(
        "TRIAGE_PROJECT_DB_MAP", json.dumps({"a": "postgresql://x@h/adb"})
    )
    # the map wins over the dbname-swap; database_name is ignored when a map entry exists
    assert (
        project_dburl("a", "ignored", "postgresql://u@h:5/base")
        == "postgresql://x@h/adb"
    )


def test_project_dburl_swaps_dbname_when_no_map(monkeypatch):
    monkeypatch.delenv("TRIAGE_PROJECT_DB_MAP", raising=False)
    assert (
        project_dburl("a", "adb", "postgresql://u@h:5432/base")
        == "postgresql://u@h:5432/adb"
    )


def test_project_dburl_raises_without_base_or_map(monkeypatch):
    monkeypatch.delenv("TRIAGE_PROJECT_DB_MAP", raising=False)
    with pytest.raises(ValueError, match="cannot resolve a database URL"):
        project_dburl("a", "adb", None)


def test_project_db_map_invalid_json_raises(monkeypatch):
    monkeypatch.setenv("TRIAGE_PROJECT_DB_MAP", "{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        project_dburl("a", "adb", "postgresql://u@h/base")


# --------------------------------------------------------------------------- integration (routing)


def _routed_app(db_url, pool, monkeypatch, *, register=True):
    upgrade_registry_db(db_url)
    # route 'proj-a' to the same test DB → pool_for resolves to the bound database and reuses the
    # default pool (proves the resolve path without needing a second database).
    monkeypatch.setenv("TRIAGE_PROJECT_DB_MAP", json.dumps({"proj-a": db_url}))
    auth = TrustedHeaderAuth(default_user="a@test", admin_emails=frozenset({"a@test"}))
    app = create_app(pool=pool, registry_pool=pool, auth_backend=auth)
    if register:
        registry.create_project(
            pool, slug="proj-a", display_name="A", database_name="proj_a_db"
        )
    return app


def test_read_route_routes_to_active_project(db_url, db_pool_greenfield, monkeypatch):
    app = _routed_app(db_url, db_pool_greenfield, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/api/experiments", headers={"X-Triage-Project": "proj-a"})
        assert r.status_code == 200  # routed to the (empty) test DB
        assert r.json() == []


def test_unknown_project_falls_back_to_default(db_url, db_pool_greenfield, monkeypatch):
    app = _routed_app(db_url, db_pool_greenfield, monkeypatch)
    with TestClient(app) as c:
        r = c.get("/api/experiments", headers={"X-Triage-Project": "ghost"})
        assert (
            r.status_code == 200
        )  # a stale/unknown selection is benign — default pool


def test_no_header_uses_default_pool(db_pool_greenfield):
    # No registry configured at all → single-project mode, unchanged.
    app = create_app(pool=db_pool_greenfield)
    with TestClient(app) as c:
        r = c.get("/api/experiments")
        assert r.status_code == 200
