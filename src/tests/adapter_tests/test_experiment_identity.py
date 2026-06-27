"""Experiment identity = the prediction PROBLEM (ADR-0022, adapters/run.py).

An Experiment is identified by ``cohort_config + label_config + temporal_config + problem_type``
— the matrix rows, the target, and the splits. features/grid/imputation are the RUN's attempt
and must NOT enter ``experiment_hash`` (adding features or models is a new run of the SAME
experiment). name/description stay display-only. These tests pin that contract at the hashing
function AND at the experiment row written by ``_create_experiment_and_run``.
"""

from __future__ import annotations

import getpass

from triage.adapters.run import _create_experiment_and_run, experiment_hash_for

_BASE_CONFIG = {
    "problem_type": "classification",
    "cohort_config": {"query": "select 1 as entity_id where {as_of_date} is not null"},
    "label_config": {"query": "select 1 as entity_id, 1 as outcome"},
    "temporal_config": {"test_durations": "6month", "label_timespans": ["6month"]},
    "feature_config": {"target": "customers"},
    "grid_config": {"sklearn.tree.DecisionTreeClassifier": {"max_depth": [3]}},
    "imputation_config": {"all": {"type": "zero"}},
}


def test_hash_ignores_name_and_description():
    """Same problem with different name/description -> the SAME experiment_hash."""
    bare = dict(_BASE_CONFIG)
    named = {**_BASE_CONFIG, "name": "Churn baseline", "description": "first try"}
    renamed = {**_BASE_CONFIG, "name": "totally different", "description": "v2 notes"}

    assert experiment_hash_for(bare) == experiment_hash_for(named)
    assert experiment_hash_for(named) == experiment_hash_for(renamed)


def test_hash_ignores_features_grid_imputation():
    """ADR-0022: adding features / models / changing imputation is the SAME experiment (a run).

    The problem (cohort+label+temporal+problem_type) is unchanged, so the hash is identical.
    """
    base = dict(_BASE_CONFIG)
    more_models = {
        **_BASE_CONFIG,
        "grid_config": {
            "sklearn.tree.DecisionTreeClassifier": {"max_depth": [3, 5, 10]},
            "sklearn.ensemble.RandomForestClassifier": {"n_estimators": [100]},
        },
    }
    more_features = {
        **_BASE_CONFIG,
        "feature_config": {"target": "customers", "extra": True},
    }
    other_impute = {**_BASE_CONFIG, "imputation_config": {"all": {"type": "mean"}}}

    assert experiment_hash_for(base) == experiment_hash_for(more_models)
    assert experiment_hash_for(base) == experiment_hash_for(more_features)
    assert experiment_hash_for(base) == experiment_hash_for(other_impute)


def test_hash_changes_with_problem():
    """ADR-0022: changing the cohort, label, temporal config, or problem_type IS a new problem."""
    base = dict(_BASE_CONFIG)
    for key, new_value in (
        ("cohort_config", {"query": "select 2 as entity_id"}),
        ("label_config", {"query": "select 1 as entity_id, 0 as outcome"}),
        (
            "temporal_config",
            {"test_durations": "12month", "label_timespans": ["12month"]},
        ),
        ("problem_type", "regression"),
    ):
        changed = {**_BASE_CONFIG, key: new_value}
        assert experiment_hash_for(base) != experiment_hash_for(changed), key


def test_create_experiment_stores_problem_config_and_cosmetics(db_pool_greenfield):
    """``_create_experiment_and_run`` keys the row by the problem hash and stores the PROBLEM
    config (cohort+label+temporal+problem_type) — not features/grid — plus name/description/author.
    """
    engine = db_pool_greenfield
    config = {**_BASE_CONFIG, "name": "Churn baseline", "description": "first try"}

    exp_hash, run_id = _create_experiment_and_run(
        engine, config, problem_type="classification", profile="local", random_seed=7
    )

    assert exp_hash == experiment_hash_for(config)
    assert exp_hash == experiment_hash_for(_BASE_CONFIG)

    with engine.connection() as conn:
        row = conn.execute(
            "select name, description, author, config from triage.experiments"
            " where experiment_hash = %(h)s",
            {"h": exp_hash},
        ).fetchone()
    assert row["name"] == "Churn baseline"
    assert row["description"] == "first try"
    assert row["author"] == getpass.getuser()
    # stored config is the PROBLEM only — features/grid/imputation/name are NOT on the experiment
    assert row["config"]["problem_type"] == "classification"
    assert "cohort_config" in row["config"] and "label_config" in row["config"]
    assert "feature_config" not in row["config"]
    assert "grid_config" not in row["config"]
    assert "name" not in row["config"]


def test_rerun_with_more_features_is_same_experiment(db_pool_greenfield):
    """A second run that ADDS features/models reuses the SAME experiment row (ADR-0022) — it is
    a new run of the same problem, not a new experiment."""
    engine = db_pool_greenfield
    first = {**_BASE_CONFIG, "name": "Original name"}
    second = {
        **_BASE_CONFIG,
        "name": "Renamed later",
        "feature_config": {"target": "customers", "extra": True},
        "grid_config": {
            "sklearn.ensemble.RandomForestClassifier": {"n_estimators": [50]}
        },
    }

    h1, run1 = _create_experiment_and_run(
        engine, first, problem_type="classification", profile="local", random_seed=1
    )
    h2, run2 = _create_experiment_and_run(
        engine, second, problem_type="classification", profile="local", random_seed=2
    )

    assert h1 == h2  # same problem -> same experiment despite different features/grid
    assert run1 != run2  # but two distinct runs
    with engine.connection() as conn:
        n_exp = conn.execute(
            "select count(*) as n from triage.experiments where experiment_hash = %(h)s",
            {"h": h1},
        ).fetchone()["n"]
        n_runs = conn.execute(
            "select count(*) as n from triage.runs where experiment_hash = %(h)s",
            {"h": h1},
        ).fetchone()["n"]
        name = conn.execute(
            "select name from triage.experiments where experiment_hash = %(h)s",
            {"h": h1},
        ).fetchone()["name"]
    assert n_exp == 1  # one experiment row, reused
    assert n_runs == 2  # two runs (attempts) under it
    assert name == "Original name"  # first writer wins (on conflict do nothing)
