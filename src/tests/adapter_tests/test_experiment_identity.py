"""Experiment identity vs. cosmetic metadata (migration 0005 + adapters/run.py).

``name``/``description`` are display-only and must NOT enter ``experiment_hash``: two configs
differing only in those keys map to the SAME experiment (identity is stable), while the human
label can change. ``author`` is the OS user, captured at creation. These tests pin that
contract at the hashing function AND at the experiment row written by ``_create_experiment_and_run``.
"""

from __future__ import annotations

import getpass

from triage.adapters.run import _create_experiment_and_run, experiment_hash_for

_BASE_CONFIG = {
    "problem_type": "classification",
    "cohort_config": {"query": "select 1 as entity_id where {as_of_date} is not null"},
    "label_config": {"query": "select 1 as entity_id, 1 as outcome"},
    "feature_config": {"target": "customers"},
    "grid_config": {"sklearn.tree.DecisionTreeClassifier": {"max_depth": [3]}},
}


def test_hash_ignores_name_and_description():
    """Same config with different name/description -> the SAME experiment_hash."""
    bare = dict(_BASE_CONFIG)
    named = {**_BASE_CONFIG, "name": "Churn baseline", "description": "first try"}
    renamed = {**_BASE_CONFIG, "name": "totally different", "description": "v2 notes"}

    assert experiment_hash_for(bare) == experiment_hash_for(named)
    assert experiment_hash_for(named) == experiment_hash_for(renamed)


def test_hash_still_changes_with_substantive_config():
    """A non-cosmetic change DOES change identity (the strip is surgical, not blanket)."""
    base = dict(_BASE_CONFIG)
    changed = {
        **_BASE_CONFIG,
        "grid_config": {"sklearn.tree.DecisionTreeClassifier": {"max_depth": [5]}},
    }
    assert experiment_hash_for(base) != experiment_hash_for(changed)


def test_create_experiment_stores_name_description_author_and_clean_config(
    db_pool_greenfield,
):
    """``_create_experiment_and_run`` writes name/description/author + the CLEANED config
    (name/description stripped) and keys the row by the cosmetic-free hash."""
    engine = db_pool_greenfield
    config = {**_BASE_CONFIG, "name": "Churn baseline", "description": "first try"}

    exp_hash, run_id = _create_experiment_and_run(
        engine, config, problem_type="classification", profile="local", random_seed=7
    )

    assert exp_hash == experiment_hash_for(config)
    # identity matches the cosmetic-free config too
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
    # the stored config has the cosmetic keys stripped
    assert "name" not in row["config"]
    assert "description" not in row["config"]
    assert row["config"]["problem_type"] == "classification"


def test_rerun_with_different_name_keeps_first_label(db_pool_greenfield):
    """A re-run of the same identity with a different name reuses the experiment row and keeps
    the FIRST writer's name (on conflict do nothing)."""
    engine = db_pool_greenfield
    first = {**_BASE_CONFIG, "name": "Original name"}
    second = {**_BASE_CONFIG, "name": "Renamed later"}

    h1, _ = _create_experiment_and_run(
        engine, first, problem_type="classification", profile="local", random_seed=1
    )
    h2, _ = _create_experiment_and_run(
        engine, second, problem_type="classification", profile="local", random_seed=2
    )

    assert h1 == h2  # same identity despite the rename
    with engine.connection() as conn:
        n = conn.execute(
            "select count(*) as n from triage.experiments where experiment_hash = %(h)s",
            {"h": h1},
        ).fetchone()["n"]
        name = conn.execute(
            "select name from triage.experiments where experiment_hash = %(h)s",
            {"h": h1},
        ).fetchone()["name"]
    assert n == 1  # one experiment row, reused
    assert name == "Original name"  # first writer wins
