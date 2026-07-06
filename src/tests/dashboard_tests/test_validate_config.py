"""Submit-form tooling tests: dry-run validation, example picker, YAML submissions (Phase 2).

``POST /api/validate-config`` is a thin wrapper over the core's
:func:`triage.adapters.run.validate_experiment_config` (ADR-0012 — validation is core logic);
these tests exercise the route contract: a real committed example validates clean with a derived
ADR-0022 hash, a broken config comes back as path-addressed structured errors, YAML parse
failures render as a verdict (not a 500), and a submission can carry the config as raw YAML
text — exactly what ``triage run`` consumes.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml
from fastapi.testclient import TestClient

from triage.adapters.run import ExperimentResult
from triage.component.registry_schema import upgrade_registry_db
from triage.dashboard.app import create_app
from triage.dashboard.auth import TrustedHeaderAuth
from triage.profiles.execution import RunHandle

ADMIN = "admin@test"
HEADERS = {"X-Triage-User": ADMIN}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
EXAMPLE_DIR = REPO_ROOT / "example"
CHI311_YAML = EXAMPLE_DIR / "chicago311" / "experiment.yaml"


class _StubRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, pool, config, *, profile="local"):
        self.calls.append({"config": config, "profile": profile})
        return RunHandle(
            run_result=ExperimentResult(
                experiment_hash="exp-stub",
                problem_type="classification",
                cohort_artifact_id="c",
                labels_artifact_id="l",
                source_pins={},
                runs=[],
            )
        )


@pytest.fixture
def client(db_url, db_pool_greenfield, monkeypatch):
    monkeypatch.setenv("TRIAGE_PROJECT_DB_MAP", json.dumps({"demo": db_url}))
    upgrade_registry_db(db_url)
    runner = _StubRunner()
    app = create_app(
        pool=db_pool_greenfield,
        registry_pool=db_pool_greenfield,
        auth_backend=TrustedHeaderAuth(
            default_user=ADMIN, admin_emails=frozenset({ADMIN})
        ),
        experiment_runner=runner,
    )
    with TestClient(app) as c:
        yield c, runner


# ------------------------------------------------------------------ validate-config


def test_validate_real_committed_example_is_clean(client):
    c, _ = client
    r = c.post(
        "/api/validate-config",
        json={"config_text": CHI311_YAML.read_text(encoding="utf-8")},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == []
    assert body["valid"] is True
    assert body["problem_type"] == "classification"
    # the ADR-0022 problem identity is derived without running anything
    assert (
        isinstance(body["experiment_hash"], str) and len(body["experiment_hash"]) == 64
    )
    assert body["n_splits"] >= 1
    assert body["n_models"] >= 1


def test_validate_broken_config_reports_path_addressed_errors(client):
    c, _ = client
    config = {
        "problem_type": "clasification",  # typo
        "cohort_config": {"query": "select entity_id from x"},  # no {as_of_date}
        # label_config + temporal_config missing entirely
        "feature_config": {"target": "t"},
        "grid_config": {},  # empty grid
    }
    r = c.post("/api/validate-config", json={"config": config}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["experiment_hash"] is None  # problem keys incomplete → no identity
    paths = {e["path"] for e in body["errors"]}
    assert "problem_type" in paths
    assert "cohort_config.query" in paths
    assert "label_config" in paths
    assert "temporal_config" in paths
    assert "grid_config" in paths
    # every error carries a human message
    assert all(e["message"] for e in body["errors"])


def test_validate_yaml_parse_error_is_a_verdict_not_a_500(client):
    c, _ = client
    r = c.post(
        "/api/validate-config",
        json={"config_text": "querty: [unclosed"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["errors"][0]["path"] == "$"


def test_validate_requires_exactly_one_config_form(client):
    c, _ = client
    assert c.post("/api/validate-config", json={}, headers=HEADERS).status_code == 400
    assert (
        c.post(
            "/api/validate-config",
            json={"config": {}, "config_text": "a: 1"},
            headers=HEADERS,
        ).status_code
        == 400
    )


# ------------------------------------------------------------------ example configs


def test_example_configs_serves_the_committed_examples(client, monkeypatch):
    monkeypatch.setenv("TRIAGE_EXAMPLES_DIR", str(EXAMPLE_DIR))
    c, _ = client
    r = c.get("/api/example-configs", headers=HEADERS)
    assert r.status_code == 200
    entries = r.json()
    names = {e["name"] for e in entries}
    assert "chicago311/experiment.yaml" in names
    for entry in entries:
        assert isinstance(yaml.safe_load(entry["content"]), dict)
        assert entry["dataset"] and entry["filename"]


def test_example_configs_empty_when_no_checkout(client, monkeypatch):
    monkeypatch.setenv("TRIAGE_EXAMPLES_DIR", "/nonexistent/anywhere")
    c, _ = client
    r = c.get("/api/example-configs", headers=HEADERS)
    assert r.status_code == 200 and r.json() == []


# ------------------------------------------------------------------ YAML submissions


_SUBMITTABLE = {
    "problem_type": "classification",
    "cohort_config": {"query": "select 1"},
    "label_config": {"query": "select 1"},
    "temporal_config": {},
    "feature_config": {"target": "t"},
    "grid_config": {"sklearn.tree.DecisionTreeClassifier": {"max_depth": [2]}},
}


def _create_demo_project(c):
    r = c.post(
        "/api/projects",
        json={"slug": "demo", "display_name": "Demo"},
        headers=HEADERS,
    )
    assert r.status_code == 201
    return r.json()


def test_project_create_reports_database_ready(client):
    c, _ = client
    project = _create_demo_project(c)
    # the env map routes 'demo' to the test DB, which carries the triage schema → ready
    assert project["database_ready"] is True


def test_submission_accepts_yaml_config_text(client):
    c, runner = client
    _create_demo_project(c)
    r = c.post(
        "/api/submissions",
        json={"project_slug": "demo", "config_text": yaml.dump(_SUBMITTABLE)},
        headers=HEADERS,
    )
    assert r.status_code == 201
    # the runner received the PARSED config — the YAML text path feeds the same seam
    assert runner.calls and runner.calls[0]["config"] == _SUBMITTABLE


def test_submission_rejects_ambiguous_config_forms(client):
    c, _ = client
    _create_demo_project(c)
    r = c.post(
        "/api/submissions",
        json={
            "project_slug": "demo",
            "config": _SUBMITTABLE,
            "config_text": yaml.dump(_SUBMITTABLE),
        },
        headers=HEADERS,
    )
    assert r.status_code == 400
