"""A forward score can reach entities the training cohort excludes (#10, ADR-0032).

``predict_forward`` rebuilt the cohort from the **training** query, which for a supervised
problem normally selects outcome-bearing rows — so it excluded, by construction, exactly the
entities forward scoring exists to score. The truncation was silent: the run completed and the
rows that mattered were simply absent.

The identity assertion here is the load-bearing one. ``_problem_identity`` hashes the whole
``cohort_config`` mapping, so had ``forward_query`` not been excluded, every experiment in every
existing database would have forked the day the key shipped.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import pytest

from tests.adapter_tests.test_run_orchestration import (
    _experiment_config,
    _seed_source,
)
from triage.adapters.forward import _forward_cohort_query, predict_forward
from triage.adapters.run import (
    experiment_hash_for,
    run_experiment,
    validate_experiment_config,
)
from triage.profiles.storage import LocalStorage

FORWARD_DATE = date(2014, 12, 1)

# The orchestration fixture's cohort is every customer. A *training* cohort that mimics the
# reported shape selects only the ones with a realized outcome; the forward cohort takes all.
TRAINING_COHORT = (
    "select customer_id as entity_id from customers"
    " where signup_date < {as_of_date}::date and customer_id <= 3"
)
FORWARD_COHORT = "select customer_id as entity_id from customers where signup_date < {as_of_date}::date"


def _config(forward_query: str | None = None) -> dict[str, Any]:
    config = copy.deepcopy(_experiment_config())
    config["cohort_config"] = {"query": TRAINING_COHORT}
    if forward_query is not None:
        config["cohort_config"]["forward_query"] = forward_query
    return config


# --------------------------------------------------------------- identity (the crux)


def test_forward_query_does_not_change_the_experiment_hash() -> None:
    """Adding the key must not fork an existing experiment (ADR-0032)."""
    without = experiment_hash_for(_config())
    with_forward = experiment_hash_for(_config(FORWARD_COHORT))
    assert without == with_forward, (
        "forward_query entered the experiment identity — every existing experiment would"
        " fork the day this key shipped"
    )


def test_changing_the_training_cohort_still_changes_identity() -> None:
    """The exclusion is surgical: the training query still defines the problem."""
    base = _config(FORWARD_COHORT)
    other = copy.deepcopy(base)
    other["cohort_config"]["query"] = TRAINING_COHORT.replace("<= 3", "<= 4")
    assert experiment_hash_for(base) != experiment_hash_for(other)


# --------------------------------------------------------------- precedence


def test_precedence_override_then_config_then_training() -> None:
    cohort_config = {"query": "TRAIN {as_of_date}", "forward_query": "FWD {as_of_date}"}
    assert _forward_cohort_query(cohort_config, "OVERRIDE") == "OVERRIDE"
    assert _forward_cohort_query(cohort_config) == "FWD {as_of_date}"
    assert (
        _forward_cohort_query({"query": "TRAIN {as_of_date}"}) == "TRAIN {as_of_date}"
    )


# --------------------------------------------------------------- validation


def test_a_forward_query_without_the_placeholder_is_rejected() -> None:
    """Rejected at validate time, so `analyze-config` catches it rather than `run` mid-flight."""
    report = validate_experiment_config(
        _config("select customer_id as entity_id from customers")
    )
    paths = [error["path"] for error in report["errors"]]
    assert "cohort_config.forward_query" in paths, (
        f"a forward cohort with no {{as_of_date}} must be rejected at validate time: {paths}"
    )
    assert report["valid"] is False


def test_a_valid_forward_query_passes_validation() -> None:
    report = validate_experiment_config(_config(FORWARD_COHORT))
    assert not [e for e in report["errors"] if "forward_query" in e["path"]]


# --------------------------------------------------------------- end to end


@pytest.fixture
def trained(db_pool_greenfield, tmp_path):
    """An experiment whose TRAINING cohort deliberately excludes customers 4-6."""
    engine = db_pool_greenfield
    _seed_source(engine)
    storage = str(tmp_path / "store")
    return engine, storage


def _latest_model_id(engine) -> int:
    with engine.connection() as conn:
        return conn.execute(
            "select model_id from triage.models"
            " order by train_end_time desc nulls last, model_id desc limit 1"
        ).fetchone()["model_id"]


def _scored_entities(engine, model_id: int) -> list[int]:
    with engine.connection() as conn:
        return sorted(
            row["entity_id"]
            for row in conn.execute(
                "select distinct entity_id from triage.predictions"
                " where model_id = %(m)s and split_kind = 'production'",
                {"m": model_id},
            ).fetchall()
        )


def test_without_forward_query_the_training_cohort_still_bounds_the_score(
    trained,
) -> None:
    """Today's behaviour, unchanged: no key means the training cohort, truncation and all."""
    engine, storage = trained
    run_experiment(
        engine, _config(), storage=LocalStorage(), storage_root=storage, random_seed=42
    )
    model_id = _latest_model_id(engine)

    predict_forward(engine, model_id, FORWARD_DATE, storage_dir=storage)

    assert _scored_entities(engine, model_id) == [1, 2, 3], (
        "the training cohort excludes customers 4-6, and without a forward cohort the"
        " forward score inherits that exclusion"
    )


def test_forward_query_reaches_the_entities_training_excluded(trained) -> None:
    """The fix: the production cohort scores the entities the training cohort could not."""
    engine, storage = trained
    run_experiment(
        engine,
        _config(FORWARD_COHORT),
        storage=LocalStorage(),
        storage_root=storage,
        random_seed=42,
    )
    model_id = _latest_model_id(engine)

    predict_forward(engine, model_id, FORWARD_DATE, storage_dir=storage)

    assert _scored_entities(engine, model_id) == [
        1,
        2,
        3,
        4,
        5,
        6,
    ], "forward_query must widen the production cohort beyond the training cohort"


def test_cli_override_beats_the_config(trained) -> None:
    """``--cohort-query`` wins over ``forward_query``, for the one-off case."""
    engine, storage = trained
    run_experiment(
        engine,
        _config(FORWARD_COHORT),
        storage=LocalStorage(),
        storage_root=storage,
        random_seed=42,
    )
    model_id = _latest_model_id(engine)

    narrow = (
        "select customer_id as entity_id from customers"
        " where signup_date < {as_of_date}::date and customer_id in (5, 6)"
    )
    predict_forward(
        engine,
        model_id,
        FORWARD_DATE,
        storage_dir=storage,
        cohort_query_template=narrow,
    )

    assert _scored_entities(engine, model_id) == [5, 6]
