"""Survival path tests (ADR-0010/0026): C-index vs the sksurv reference + the fit seam.

The discipline mirrors ``catwalk_tests/test_in_pg_metrics.py`` (the ADR-0007 proof): the
PL/pgSQL ``triage.c_index`` (migration 0011) must equal
``sksurv.metrics.concordance_index_censored`` on randomized fixtures that deliberately
include score ties, duration ties, and heavy censoring. The fit seam test proves
``adapters.model._fit_estimator`` routes a scikit-survival estimator onto the structured
``(event_observed, duration)`` label pair and that its risk scores flow through
``_score_column`` unchanged (the ADR-0010 ranking spine).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest
from sksurv.metrics import concordance_index_censored

from triage.adapters.matrix import MatrixResult
from triage.adapters.model import _fit_estimator, _score_column
from triage.adapters.run import _resolve_metric_config, validate_experiment_config
from triage.component.catwalk.in_pg_evaluation import (
    DEFAULT_CLASSIFICATION_CONFIG,
    DEFAULT_REGRESSION_CONFIG,
    DEFAULT_SURVIVAL_CONFIG,
    evaluate_in_db,
)
from triage.profiles.storage import LocalStorage, write_parquet

LABEL_TIMESPAN = "6 months"
AS_OF_DATE = "2014-01-01"


# ------------------------------------------------------------------ DB seeding
# Mirrors test_in_pg_metrics: the minimal FK chain a prediction needs, plus
# SURVIVAL labels (duration + event_observed; outcome stays NULL).


def _seed_model(pool):
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type) "
            "values ('exp-surv', '{}'::jsonb, 'survival')"
        )
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('model-art-s', 'model-logical-s', 'model', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.model_groups "
            "(model_group_hash, model_type, hyperparameters, feature_list) "
            "values ('mg-surv', 'sksurv.linear_model.CoxPHSurvivalAnalysis',"
            " '{}'::jsonb, ARRAY['x1','x2'])"
        )
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, train_end_time) "
            "select model_group_id, 'model-art-s', date '2013-07-01' "
            "from triage.model_groups where model_group_hash = 'mg-surv' "
            "returning model_id"
        ).fetchone()["model_id"]
    return model_id


def _seed_survival_predictions_and_labels(pool, model_id, scores, durations, events):
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('labels-art-s', 'labels-logical-s', 'labels', '{}'::jsonb)"
        )
        for eid, (score, duration, event) in enumerate(
            zip(scores, durations, events), start=1
        ):
            conn.execute(
                "insert into triage.predictions "
                "(model_id, entity_id, as_of_date, split_kind, score) "
                "values (%(m)s, %(e)s, %(d)s, 'test', %(s)s)",
                {"m": model_id, "e": eid, "d": AS_OF_DATE, "s": float(score)},
            )
            conn.execute(
                "insert into triage.labels (label_hash, entity_id, as_of_date,"
                " label_timespan, duration, event_observed) "
                "values ('labels-art-s', %(e)s, %(d)s, cast(%(ts)s as interval),"
                " %(dur)s, %(ev)s)",
                {
                    "e": eid,
                    "d": AS_OF_DATE,
                    "ts": LABEL_TIMESPAN,
                    "dur": float(duration),
                    "ev": bool(event),
                },
            )


def _sql_c_index(pool, model_id):
    with pool.connection() as conn:
        return conn.execute(
            "select (triage.c_index(%(m)s, 'test', %(d)s::date,"
            " cast(%(ts)s as interval))).*",
            {"m": model_id, "d": AS_OF_DATE, "ts": LABEL_TIMESPAN},
        ).fetchone()


# ------------------------------------------------------------------ C-index reference


@pytest.mark.parametrize("seed", [7, 23, 91])
def test_c_index_matches_sksurv_reference(db_pool_greenfield, seed):
    """triage.c_index == concordance_index_censored on randomized data WITH ties + censoring."""
    rng = np.random.default_rng(seed)
    n = 200
    durations = rng.integers(1, 40, n).astype(float)  # heavy duration ties
    events = rng.random(n) < 0.6  # ~40% censored
    scores = np.round(rng.random(n), 1)  # heavy score ties
    # near-ties around sksurv's tied_tol=1e-8: within-tolerance counts as TIED, just
    # outside doesn't — locks the tolerance parity a continuous-score model exposed live
    scores[1] = scores[0] + 5e-9
    scores[3] = scores[2] + 5e-8

    model_id = _seed_model(db_pool_greenfield)
    _seed_survival_predictions_and_labels(
        db_pool_greenfield, model_id, scores, durations, events
    )

    row = _sql_c_index(db_pool_greenfield, model_id)
    expected = concordance_index_censored(events, durations, scores)[0]

    assert row["num_labeled"] == n
    assert row["num_positive"] == int(events.sum())
    assert row["value"] == pytest.approx(expected, abs=1e-9)
    assert row["value_expected"] == pytest.approx(0.5)


def test_c_index_perfect_and_random_orientations(db_pool_greenfield):
    """Higher score = higher risk = earlier event: a perfectly anti-ordered ranking scores 1."""
    durations = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    events = np.array([True, True, True, True, True])
    scores = np.array([0.9, 0.8, 0.6, 0.4, 0.2])  # earliest failure ranked first

    model_id = _seed_model(db_pool_greenfield)
    _seed_survival_predictions_and_labels(
        db_pool_greenfield, model_id, scores, durations, events
    )
    row = _sql_c_index(db_pool_greenfield, model_id)
    assert row["value"] == pytest.approx(1.0)
    assert row["value_best"] == 1.0 and row["value_worst"] == 0.0


def test_c_index_undefined_without_events(db_pool_greenfield):
    """All-censored data has no comparable pairs — value stays NULL, counts populated."""
    durations = np.array([3.0, 5.0, 9.0])
    events = np.array([False, False, False])
    scores = np.array([0.1, 0.5, 0.9])

    model_id = _seed_model(db_pool_greenfield)
    _seed_survival_predictions_and_labels(
        db_pool_greenfield, model_id, scores, durations, events
    )
    row = _sql_c_index(db_pool_greenfield, model_id)
    assert row["value"] is None
    assert row["num_labeled"] == 3 and row["num_positive"] == 0


def test_evaluate_model_dispatches_survival_metrics(db_pool_greenfield):
    """The 0011 dispatcher writes a c_index evaluations row from survival_metrics config."""
    rng = np.random.default_rng(5)
    n = 50
    durations = rng.integers(1, 30, n).astype(float)
    events = rng.random(n) < 0.7
    scores = rng.random(n)

    model_id = _seed_model(db_pool_greenfield)
    _seed_survival_predictions_and_labels(
        db_pool_greenfield, model_id, scores, durations, events
    )
    written = evaluate_in_db(
        db_pool_greenfield,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config=DEFAULT_SURVIVAL_CONFIG,
    )
    assert written == 1
    with db_pool_greenfield.connection() as conn:
        row = conn.execute(
            "select value, num_labeled from triage.evaluations "
            "where model_id = %(m)s and metric = 'c_index'",
            {"m": model_id},
        ).fetchone()
    expected = concordance_index_censored(events, durations, scores)[0]
    assert row["value"] == pytest.approx(expected, abs=1e-9)
    assert row["num_labeled"] == n


# ------------------------------------------------------------------ fit seam


def _survival_matrix(tmp_path, n=120, seed=3):
    """A small survival train matrix Parquet + its MatrixResult."""
    rng = np.random.default_rng(seed)
    x1 = rng.normal(size=n)
    x2 = rng.normal(size=n)
    # risk increases with x1: time drawn with rate exp(x1) so higher x1 → earlier event
    times = rng.exponential(scale=np.exp(-x1)) * 30 + 1
    events = rng.random(n) < 0.7
    frame = pl.DataFrame(
        {
            "entity_id": pl.Series(range(1, n + 1), dtype=pl.Int64),
            "as_of_date": [date(2014, 1, 1)] * n,
            "x1": x1,
            "x2": x2,
            "duration": times,
            "event_observed": events,
        }
    )
    # one unlabeled row (NULL survival pair) — must be dropped by the fit, not crash it
    frame = frame.with_columns(
        pl.when(pl.col("entity_id") == 1)
        .then(None)
        .otherwise(pl.col("duration"))
        .alias("duration")
    )
    uri = str(tmp_path / "survival-train.parquet")
    write_parquet(LocalStorage(), uri, frame)
    return MatrixResult(
        matrix_artifact_id="m-art-surv",
        feature_group_artifact_id="fg-art-surv",
        storage_uri=uri,
        num_entities=n,
        num_features=2,
        feature_names=["x1", "x2"],
        fit_based_stats={},
        cache_hit=False,
    )


def test_fit_estimator_survival_branch_cox(tmp_path):
    """A sksurv estimator fits on Surv(event, duration) and predicts finite risk scores."""
    matrix_result = _survival_matrix(tmp_path)
    estimator, feature_columns = _fit_estimator(
        matrix_result, "sksurv.linear_model.CoxPHSurvivalAnalysis", {}, random_seed=0
    )
    assert feature_columns == ["x1", "x2"]
    x = np.column_stack([np.linspace(-1, 1, 5), np.zeros(5)])
    risks = _score_column(estimator, x)
    assert risks.shape == (5,)
    assert np.all(np.isfinite(risks))
    # higher x1 was built to mean higher risk — the Cox coefficient must recover the sign,
    # so the risk column ranks high-x1 rows first (the ADR-0010 spine orientation).
    assert risks[-1] > risks[0]


def test_fit_estimator_survival_requires_label_pair(tmp_path):
    """A survival estimator on a matrix without the (duration, event_observed) pair fails loud."""
    frame = pl.DataFrame(
        {
            "entity_id": [1, 2],
            "as_of_date": [date(2014, 1, 1)] * 2,
            "x1": [0.1, 0.2],
            "outcome": [0.0, 1.0],
        }
    )
    uri = str(tmp_path / "not-survival.parquet")
    write_parquet(LocalStorage(), uri, frame)
    matrix_result = MatrixResult(
        matrix_artifact_id="m-art-ns",
        feature_group_artifact_id="fg-art-ns",
        storage_uri=uri,
        num_entities=2,
        num_features=1,
        feature_names=["x1"],
        fit_based_stats={},
        cache_hit=False,
    )
    with pytest.raises(ValueError, match="duration"):
        _fit_estimator(
            matrix_result,
            "sksurv.linear_model.CoxPHSurvivalAnalysis",
            {},
            random_seed=0,
        )


# ------------------------------------------------------------------ metric-config resolution


def test_metric_config_defaults_follow_problem_type():
    assert (
        _resolve_metric_config({}, "classification", None)
        == DEFAULT_CLASSIFICATION_CONFIG
    )
    assert _resolve_metric_config({}, "regression", None) == DEFAULT_REGRESSION_CONFIG
    assert (
        _resolve_metric_config({}, "regression_ranking", None)
        == DEFAULT_REGRESSION_CONFIG
    )
    assert _resolve_metric_config({}, "survival", None) == DEFAULT_SURVIVAL_CONFIG


def test_metric_config_evaluation_block_and_override_precedence():
    block = {"regression_metrics": ["rmse", "mae"]}
    config = {"evaluation": block}
    assert _resolve_metric_config(config, "regression", None) == block
    # an explicit argument (CLI/tests) beats the config block
    override = {"metrics": ["auc_roc"]}
    assert _resolve_metric_config(config, "regression", override) == override


def test_migration_0011_roundtrip(db_url, db_pool_greenfield):
    """0011 downgrade removes c_index/survival_ranks and restores the 0002 evaluate_model."""
    from triage.component.results_schema import downgrade_db, upgrade_db

    def _has(conn, signature):
        return conn.execute(
            "select to_regprocedure(%(sig)s) is not null as f", {"sig": signature}
        ).fetchone()["f"]

    c_index_sig = "triage.c_index(bigint, triage.split_kind, date, interval)"
    eval_sig = (
        "triage.evaluate_model(bigint, triage.split_kind, date, interval, jsonb, text)"
    )
    with db_pool_greenfield.connection() as conn:
        assert _has(conn, c_index_sig)

    # below 0011 explicitly (head keeps moving — "-1" would only undo the newest migration)
    downgrade_db(dburl=db_url, revision="0010_windowed_evaluations")
    with db_pool_greenfield.connection() as conn:
        assert not _has(conn, c_index_sig)
        assert _has(conn, eval_sig)  # the 0002 dispatcher body is restored, not dropped

    upgrade_db(dburl=db_url, revision="head")
    with db_pool_greenfield.connection() as conn:
        assert _has(conn, c_index_sig)


def test_validator_flags_survival_without_the_extra(monkeypatch):
    """problem_type survival + no sksurv → a config-validation error naming the extra."""
    import importlib.util as ilu

    real_find_spec = ilu.find_spec
    monkeypatch.setattr(
        ilu,
        "find_spec",
        lambda name, *a, **k: (
            None if name == "sksurv" else real_find_spec(name, *a, **k)
        ),
    )
    result = validate_experiment_config({"problem_type": "survival"})
    assert any(
        "survival extra" in e["message"] and e["path"] == "problem_type"
        for e in result["errors"]
    )
