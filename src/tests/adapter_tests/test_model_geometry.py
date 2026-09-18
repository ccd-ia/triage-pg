"""The fit geometry contract (#14): a model is scored in the order it was fitted.

``run.py`` projects every model onto a **sorted** feature list — ``_feature_subsets`` sorts even
when the config declares no ``feature_groups`` block — while ``triage.matrices.feature_names``
records the matrix's **build** order. ``predict_forward`` used to score against the latter, so an
estimator fitted on one column order was fed another. Nothing raised: the column *sets* are
identical, so ``_design_X``'s missing-column check passes and ``frame.select()`` silently
transposes the design matrix.

**The fixture is the whole test.** The orchestration fixture's nine feature labels are already in
sorted order (``COUNT(…) < MEAN(…) < SUM(…) < age`` in ASCII), and featurizer 1.1.0 emits its
plain aggregation labels sorted — so with that config the two orders coincide and the bug is
invisible by construction. That is exactly why three existing forward tests never caught it.

Every test here therefore adds one **one-hot expanded boolean**, which is the mechanism the issue
actually reports: featurizer expands a ``role: categorical`` variable into entity-prefixed columns
(``customers.is_vip=false``, ``customers.is_vip=true``) and emits them *before* the plain
passthrough ``age`` — while they sort *after* it, because ``c`` (99) > ``a`` (97). The result is a
three-column rotation at the tail, the miniature of the ten-column rotation over 141 the issue
measured on one-hot ``game_wide.*`` columns.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import polars as pl
import pytest

from tests.adapter_tests.test_run_orchestration import (
    _experiment_config,
    _seed_source,
)
from triage.adapters.forward import predict_forward
from triage.adapters.model import _load_estimator
from triage.adapters.retrain import retrain
from triage.adapters.run import run_experiment
from triage.profiles.storage import LocalStorage

FORWARD_DATE = date(2014, 12, 1)

#: Emitted before ``age`` (one-hot columns lead the passthroughs), sorts after it. See the
#: module docstring — this is what makes build order differ from the fitted, sorted order.
ROTATING_COLUMN = "customers.is_vip=true"


def _rotated_config() -> dict[str, Any]:
    """The orchestration config plus a one-hot boolean, so build order != sorted order.

    A linear estimator replaces the decision tree: a shallow tree need not split on the columns a
    rotation moves, so it can agree under both orders by luck. A logistic regression cannot — the
    one-hot columns are 0/1 while ``age`` is ~30-36, so transposing them changes every score.
    """
    config = copy.deepcopy(_experiment_config())
    config["feature_config"]["entities"][0]["variables"]["is_vip"] = {
        "type": "boolean",
        "role": "categorical",
        "vocabulary": ["true", "false"],
    }
    config["grid_config"] = {
        "sklearn.linear_model.LogisticRegression": {"C": [1.0], "max_iter": [500]}
    }
    return config


def _seed_rotated(engine) -> None:
    """Seed the orchestration source, then add the one-hot source column."""
    _seed_source(engine)
    with engine.connection() as conn:
        conn.execute("alter table customers add column is_vip boolean")
        conn.execute("update customers set is_vip = (customer_id % 2 = 0)")


def _model_row(engine, model_id: int) -> dict[str, Any]:
    with engine.connection() as conn:
        return conn.execute(
            "select m.model_id, m.model_group_id, m.feature_list, m.artifact_uri,"
            + " m.train_matrix_uuid, mx.feature_names, mx.storage_uri"
            + " from triage.models m"
            + " join triage.matrices mx on mx.matrix_uuid = m.train_matrix_uuid"
            + " where m.model_id = %(mid)s",
            {"mid": model_id},
        ).fetchone()


def _latest_model_id(engine) -> int:
    with engine.connection() as conn:
        return conn.execute(
            "select model_id from triage.models"
            + " order by train_end_time desc nulls last, model_id desc limit 1"
        ).fetchone()["model_id"]


@pytest.fixture
def rotated_experiment(db_pool_greenfield, tmp_path):
    """A real experiment whose matrix build order is NOT its sorted order."""
    engine = db_pool_greenfield
    _seed_rotated(engine)
    storage = str(tmp_path / "store")
    run_experiment(
        engine,
        _rotated_config(),
        storage=LocalStorage(),
        storage_root=storage,
        random_seed=42,
    )
    return engine, storage


def test_the_fixture_actually_rotates(rotated_experiment) -> None:
    """Guard on the guard: if this fails, every other test here proves nothing.

    A fixture whose build order equals its sorted order cannot detect a transposition, which is
    precisely why the bug survived three existing forward tests.
    """
    engine, _ = rotated_experiment
    row = _model_row(engine, _latest_model_id(engine))

    build_order = list(row["feature_names"])
    assert ROTATING_COLUMN in build_order
    assert build_order != sorted(build_order), (
        "the matrix build order is already sorted, so this suite cannot detect a"
        " train/score transposition — fix the fixture before trusting any result here"
    )
    # The one-hot columns lead "age" on the way out and trail it once sorted.
    assert build_order.index(ROTATING_COLUMN) < build_order.index("age")
    assert sorted(build_order).index(ROTATING_COLUMN) > sorted(build_order).index("age")


def test_build_model_records_the_order_it_fitted(rotated_experiment) -> None:
    """``triage.models.feature_list`` is the fit order, not the build order (migration 0021)."""
    engine, _ = rotated_experiment
    row = _model_row(engine, _latest_model_id(engine))

    recorded = list(row["feature_list"])
    build_order = list(row["feature_names"])

    assert recorded, "models.feature_list must be populated at fit time"
    assert recorded == sorted(build_order), "the run path fits on the sorted projection"
    assert recorded != build_order, "and that is not the matrix's build order"


def test_forward_scores_in_the_recorded_order(rotated_experiment) -> None:
    """The regression test for #14: stored scores match the recorded geometry, not build order.

    Replicates the issue's own repro — score the stored matrix through each candidate column
    order and compare against what the pipeline persisted.
    """
    engine, storage = rotated_experiment
    model_id = _latest_model_id(engine)
    row = _model_row(engine, model_id)

    result = predict_forward(engine, model_id, FORWARD_DATE, storage_dir=storage)
    assert result.num_predictions > 0

    with engine.connection() as conn:
        stored = {
            r["entity_id"]: r["score"]
            for r in conn.execute(
                "select entity_id, score from triage.predictions"
                + " where model_id = %(mid)s and split_kind = 'production'"
                + " and as_of_date = %(d)s",
                {"mid": model_id, "d": FORWARD_DATE},
            ).fetchall()
        }
        production_uri = conn.execute(
            "select mx.storage_uri from triage.matrices mx"
            + " join triage.predictions p on p.matrix_uuid = mx.matrix_uuid"
            + " where p.model_id = %(mid)s and p.split_kind = 'production' limit 1",
            {"mid": model_id},
        ).fetchone()["storage_uri"]

    frame = pl.read_parquet(production_uri)
    estimator = _load_estimator(row["artifact_uri"])
    entities = frame.get_column("entity_id").to_list()

    fit_order = list(row["feature_list"])
    build_order = list(row["feature_names"])

    def max_error(order: list[str]) -> float:
        probs = estimator.predict_proba(frame.select(order).to_numpy())[:, 1]
        return max(abs(probs[i] - stored[e]) for i, e in enumerate(entities))

    # The recorded geometry reproduces the pipeline exactly.
    assert max_error(fit_order) == pytest.approx(0.0, abs=1e-12)
    # And the build order — what forward.py used to pass — does not.
    assert max_error(build_order) > 1e-6, (
        "scoring through the build order agrees with the stored scores, so this fixture's"
        " rotation does not reach the estimator — the assertion above proves nothing"
    )


def test_retrain_fits_on_the_group_geometry(rotated_experiment) -> None:
    """A retrained model is fitted on its group's spec, in the group's order (#14 sibling).

    ``retrain`` builds a full matrix with no feature-group projection, so before the fix it fitted
    on the build order and then rejoined its group anyway — ``_model_group_hash`` sorts the list
    before hashing, so the mismatch was invisible in the group identity.
    """
    engine, storage = rotated_experiment
    original_id = _latest_model_id(engine)
    original = _model_row(engine, original_id)

    result = retrain(
        engine,
        original["model_group_id"],
        date(2014, 10, 1),
        storage_dir=storage,
    )

    retrained = _model_row(engine, result.retrain_model_id)
    assert list(retrained["feature_list"]) == list(original["feature_list"]), (
        "a retrained model must be fitted on the same ordered geometry as its group"
    )
    assert list(retrained["feature_list"]) != list(retrained["feature_names"]), (
        "and that geometry is not the retrain matrix's build order"
    )
