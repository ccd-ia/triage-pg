"""Write-surface contract tests: registry projects + experiment submissions (ADR-0002/0024).

The write half of the dashboard app (``triage.dashboard.write_routes``) over both the ``triage``
results schema (the project pool) and the ``registry`` control plane. Both schemas are applied to
ONE throwaway test DB (distinct alembic version tables let them coexist), and the same pool serves
as project + registry pool — sufficient because the routes address them by schema-qualified name.

The experiment runner is stubbed (``_StubRunner``) so a submission exercises the full route —
authz → run → record — without a real training run. Auth is an explicit ``TrustedHeaderAuth`` with
a fixed admin so the tests don't depend on ambient ``TRIAGE_*`` env.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from triage.adapters.run import ExperimentResult
from triage.component.registry_schema import upgrade_registry_db
from triage.dashboard.app import create_app
from triage.dashboard.auth import TrustedHeaderAuth
from triage.profiles.execution import RunHandle

ADMIN = "admin@test"
_ADMIN_HEADERS = {"X-Triage-User": ADMIN}


class _StubRunner:
    """Records its calls and returns a canned :class:`RunHandle` (local or cloud shape)."""

    def __init__(self, *, cloud: bool = False):
        self.calls: list[dict] = []
        self._cloud = cloud

    def __call__(self, pool, config, *, profile="local"):
        self.calls.append({"pool": pool, "config": config, "profile": profile})
        if self._cloud:
            return RunHandle(
                batch_job_id="job-123", config_uri="s3://bucket/config.json"
            )
        result = ExperimentResult(
            experiment_hash="exp-stub",
            problem_type="classification",
            cohort_artifact_id="cohort-stub",
            labels_artifact_id="labels-stub",
            source_pins={},
            runs=[],
        )
        return RunHandle(run_result=result)


_VALID_CONFIG = {
    "problem_type": "classification",
    "cohort_config": {"query": "select 1"},
    "label_config": {"query": "select 1"},
    "temporal_config": {},
    "feature_config": {"target": "t"},
    "grid_config": {"sklearn.tree.DecisionTreeClassifier": {"max_depth": [2]}},
}


def _make_client(db_url, db_pool_greenfield, *, cloud=False):
    """App over the shared test DB (triage + registry schemas), stub runner, fixed admin auth."""
    upgrade_registry_db(db_url)
    runner = _StubRunner(cloud=cloud)
    auth = TrustedHeaderAuth(default_user=ADMIN, admin_emails=frozenset({ADMIN}))
    app = create_app(
        pool=db_pool_greenfield,
        registry_pool=db_pool_greenfield,
        auth_backend=auth,
        experiment_runner=runner,
    )
    return app, runner


@pytest.fixture
def write_client(db_url, db_pool_greenfield, monkeypatch):
    # Route the 'food' project (the submit tests' target) to the SAME test DB, so pool_for_slug
    # (ADR-0025) resolves to the bound database and reuses the default pool — no second DB needed.
    monkeypatch.setenv("TRIAGE_PROJECT_DB_MAP", json.dumps({"food": db_url}))
    app, runner = _make_client(db_url, db_pool_greenfield)
    with TestClient(app) as c:
        yield c, runner


# --------------------------------------------------------------------------- identity + authz


def test_me_returns_admin_identity(write_client):
    client, _ = write_client
    r = client.get("/api/me", headers=_ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == ADMIN
    assert body["is_admin"] is True


def test_me_default_user_when_no_header(write_client):
    client, _ = write_client
    # No header → the backend's default_user (which is the admin here).
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["email"] == ADMIN


def test_non_admin_is_not_admin(write_client):
    client, _ = write_client
    r = client.get("/api/me", headers={"X-Triage-User": "someone@else.com"})
    assert r.status_code == 200
    assert r.json()["is_admin"] is False


# --------------------------------------------------------------------------- projects


def test_create_and_list_project(write_client):
    client, _ = write_client
    r = client.post(
        "/api/projects",
        json={"slug": "food", "display_name": "Food Inspections"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text
    project = r.json()
    assert project["slug"] == "food"
    assert project["database_name"] == "food"  # defaults to the slug

    listed = client.get("/api/projects", headers=_ADMIN_HEADERS).json()
    assert [p["slug"] for p in listed] == ["food"]

    # the creator is auto-added as owner
    members = client.get("/api/projects/food/members", headers=_ADMIN_HEADERS).json()
    assert len(members) == 1
    assert members[0]["email"] == ADMIN
    assert members[0]["role"] == "owner"


def test_create_project_requires_admin(write_client):
    client, _ = write_client
    r = client.post(
        "/api/projects",
        json={"slug": "x", "display_name": "X"},
        headers={"X-Triage-User": "nonadmin@test"},
    )
    assert r.status_code == 403


def test_create_project_rejects_bad_slug(write_client):
    client, _ = write_client
    r = client.post(
        "/api/projects",
        json={"slug": "Not A Slug!", "display_name": "X"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 400
    assert "slug" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- submissions


def _create_project(client, slug="food"):
    return client.post(
        "/api/projects",
        json={"slug": slug, "display_name": slug.title()},
        headers=_ADMIN_HEADERS,
    )


def test_submit_experiment_runs_and_records(write_client):
    client, runner = write_client
    _create_project(client)

    r = client.post(
        "/api/submissions",
        json={"project_slug": "food", "config": _VALID_CONFIG, "profile": "local"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # the stub runner was invoked with the project pool + config
    assert len(runner.calls) == 1
    assert runner.calls[0]["config"] == _VALID_CONFIG
    assert runner.calls[0]["profile"] == "local"
    # the run summary + audit row
    assert body["result"]["experiment_hash"] == "exp-stub"
    assert body["submission"]["experiment_hash"] == "exp-stub"
    assert body["submission"]["profile"] == "local"

    # it shows up in the audit trail (scoped to the project)
    subs = client.get(
        "/api/submissions?project_slug=food", headers=_ADMIN_HEADERS
    ).json()
    assert len(subs) == 1
    assert subs[0]["experiment_hash"] == "exp-stub"
    assert subs[0]["submitted_by_email"] == ADMIN
    assert subs[0]["project_slug"] == "food"


def test_submit_unknown_project_404(write_client):
    client, runner = write_client
    r = client.post(
        "/api/submissions",
        json={"project_slug": "ghost", "config": _VALID_CONFIG},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 404
    assert runner.calls == []  # never ran


def test_submit_missing_config_key_400(write_client):
    client, runner = write_client
    _create_project(client)
    bad = {k: v for k, v in _VALID_CONFIG.items() if k != "label_config"}
    r = client.post(
        "/api/submissions",
        json={"project_slug": "food", "config": bad},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 400
    assert "label_config" in r.json()["detail"]
    assert runner.calls == []  # validation happens before the run


def test_submit_non_member_forbidden(write_client):
    client, runner = write_client
    _create_project(client)  # owned by ADMIN
    r = client.post(
        "/api/submissions",
        json={"project_slug": "food", "config": _VALID_CONFIG},
        headers={"X-Triage-User": "outsider@test"},  # not a member, not admin
    )
    assert r.status_code == 403
    assert runner.calls == []


def test_cloud_submission_records_batch_job(db_url, db_pool_greenfield, monkeypatch):
    monkeypatch.setenv("TRIAGE_PROJECT_DB_MAP", json.dumps({"food": db_url}))
    app, runner = _make_client(db_url, db_pool_greenfield, cloud=True)
    with TestClient(app) as client:
        _create_project(client)
        r = client.post(
            "/api/submissions",
            json={"project_slug": "food", "config": _VALID_CONFIG, "profile": "cloud"},
            headers=_ADMIN_HEADERS,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["result"]["batch_job_id"] == "job-123"
        assert body["result"]["status"] == "submitted"
        assert body["submission"]["batch_job_id"] == "job-123"
        assert (
            body["submission"]["experiment_hash"] is None
        )  # not known until the job runs


# --------------------------------------------------------------------------- registry not configured


def test_write_routes_503_without_registry(db_pool_greenfield, monkeypatch):
    """With no registry pool (and no TRIAGE_REGISTRY_URL), the write surface 503s cleanly."""
    monkeypatch.delenv("TRIAGE_REGISTRY_URL", raising=False)
    auth = TrustedHeaderAuth(default_user=ADMIN, admin_emails=frozenset({ADMIN}))
    app = create_app(pool=db_pool_greenfield, auth_backend=auth)  # no registry_pool
    with TestClient(app) as client:
        r = client.get("/api/projects", headers=_ADMIN_HEADERS)
        assert r.status_code == 503
        assert "registry" in r.json()["detail"].lower()
