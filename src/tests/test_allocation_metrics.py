"""The allocation metrics of migration 0035: lift@k, captured@k, aucc, spearman, gains_curve.

One regression_ranking Experiment, scored from one matrix. On R1, 50 entities are scored and
the first 40 are labeled with a non-negative target that has zeros and ties. Four models rank
them: ``perfect`` (the outcome itself, the unlabeled rows last), ``reversed``, ``noisy`` (the
outcome plus noise, no ties) and ``constant`` (one score). R2 holds a signed target and R0 an
all-zero one, where the shares are undefined.

Each value is checked against a numpy reference written here, row by row, not against the
audit's implementation.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import date
from typing import Any

import numpy as np
import pytest
from scipy.stats import spearmanr as _spearmanr

from triage.component.catwalk.in_pg_evaluation import (
    DEFAULT_REGRESSION_RANKING_CONFIG,
    evaluate_in_db,
)
from triage.component.results_schema import downgrade_db, upgrade_db
from triage.diagnostics import metric_audit


def spearmanr(a: np.ndarray, b: np.ndarray) -> float:
    """scipy's rho (scipy ships no type hints, so the result is read untyped)."""
    result: Any = _spearmanr(a, b)
    return float(result.statistic)


LABEL_TIMESPAN = "6 months"
R1, R2, R0 = date(2016, 1, 1), date(2016, 2, 1), date(2016, 3, 1)
N, N_LABELED = 50, 40
#: The default thresholds and two more, a small absolute cut and half the population.
THRESHOLDS = [*DEFAULT_REGRESSION_RANKING_CONFIG["thresholds"], "5_abs", "50_pct"]

_RNG = np.random.default_rng(11)
OUTCOME = np.concatenate([_RNG.gamma(1.5, 20.0, 30).round(0), np.zeros(10)])
SIGNED = OUTCOME - 15.0
NOISE = _RNG.normal(0, 8, N)

#: The four rankings of R1, as the score of every entity (1..50; 41..50 unlabeled).
_LABELED_SCORE = {
    "perfect": OUTCOME,
    "reversed": -OUTCOME,
    "noisy": OUTCOME + NOISE[:N_LABELED],
    "constant": np.ones(N_LABELED),
}
_UNLABELED_SCORE = {
    "perfect": np.full(10, -1000.0),
    "reversed": np.full(10, -1000.0),
    "noisy": NOISE[N_LABELED:] - 50.0,
    "constant": np.ones(10),
}


def _scores(name: str) -> np.ndarray:
    return np.concatenate([_LABELED_SCORE[name], _UNLABELED_SCORE[name]])


def _outcomes(target: np.ndarray) -> np.ndarray:
    """The population's outcomes: the labeled target, then NaN for the unlabeled rows."""
    return np.concatenate([target, np.full(N - N_LABELED, np.nan)])


def seed_allocation(pool) -> dict[str, int]:
    """Four regression_ranking models scored on R1, R2 and R0, evaluated with the defaults."""
    matrix_uuid = str(uuid.uuid4())
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-alloc', '{}'::jsonb, 'regression_ranking')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile)"
            " values ('exp-alloc', 'local') returning run_id"
        ).fetchone()["run_id"]
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " ('alloc-labels', 'alloc-labels', 'labels', '{}'::jsonb),"
            " ('alloc-matrix', 'alloc-matrix', 'matrix', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values ('alloc-matrix', 'alloc-labels')"
        )
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, label_timespan) values (%(u)s, 'alloc-matrix', 'test',"
            " 'mem://a', cast(%(ts)s as interval))",
            {"u": matrix_uuid, "ts": LABEL_TIMESPAN},
        )
        ids: dict[str, int] = {}
        for name in _LABELED_SCORE:
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
                {"a": f"alloc-{name}"},
            )
            gid = conn.execute(
                "insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list)"
                " values (%(h)s, 'sklearn.linear_model.LinearRegression', '{}'::jsonb,"
                " array['f']) returning model_group_id",
                {"h": f"mg-alloc-{name}"},
            ).fetchone()["model_group_id"]
            ids[name] = conn.execute(
                "insert into triage.models (model_group_id, model_hash, run_id,"
                " train_end_time, training_label_timespan)"
                " values (%(g)s, %(a)s, %(r)s, date '2015-12-01',"
                " cast(%(ts)s as interval)) returning model_id",
                {"g": gid, "a": f"alloc-{name}", "r": run_id, "ts": LABEL_TIMESPAN},
            ).fetchone()["model_id"]
        with conn.cursor() as cur:
            for as_of, target in (
                (R1, OUTCOME),
                (R2, SIGNED),
                (R0, np.zeros(N_LABELED)),
            ):
                cur.executemany(
                    "insert into triage.labels (label_hash, entity_id, as_of_date,"
                    " label_timespan, outcome) values ('alloc-labels', %(e)s, %(d)s,"
                    " cast(%(ts)s as interval), %(o)s)",
                    [
                        {"e": e, "d": as_of, "ts": LABEL_TIMESPAN, "o": float(o)}
                        for e, o in enumerate(target, start=1)
                    ],
                )
                for name, model_id in ids.items():
                    cur.executemany(
                        "insert into triage.predictions (model_id, entity_id, as_of_date,"
                        " split_kind, score, matrix_uuid)"
                        " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s)",
                        [
                            {"m": model_id, "e": e, "d": as_of, "s": float(s),
                             "u": matrix_uuid}
                            for e, s in enumerate(_scores(name), start=1)
                        ],
                    )  # fmt: skip
    config = {**DEFAULT_REGRESSION_RANKING_CONFIG, "thresholds": THRESHOLDS}
    for model_id in ids.values():
        for as_of in (R1, R2, R0):
            evaluate_in_db(pool, model_id, as_of, LABEL_TIMESPAN, metric_config=config)
    return ids


@pytest.fixture
def models(db_pool_greenfield) -> dict[str, int]:
    return seed_allocation(db_pool_greenfield)


def _stored(pool, model_id: int, metric: str, parameter: str = "", as_of: date = R1):
    with pool.connection() as conn:
        row = conn.execute(
            "select value, num_labeled, num_labeled_above_threshold"
            " from triage.evaluations where model_id = %(m)s and metric = %(metric)s"
            " and parameter = %(p)s and as_of_date = %(d)s and subset_hash = ''",
            {"m": model_id, "metric": metric, "p": parameter, "d": as_of},
        ).fetchone()
    assert row is not None, (model_id, metric, parameter, as_of)
    return row


# ---------------------------------------------------------------- the numpy references


def _k(parameter: str, n: int) -> int:
    raw, unit = parameter.split("_")
    return int(raw) if unit == "abs" else math.ceil(float(raw) / 100 * n)


def _ranked(scores: np.ndarray, outcomes: np.ndarray) -> np.ndarray:
    """Outcomes in population rank order: score descending, entity_id ascending."""
    entity = np.arange(1, len(scores) + 1)
    return outcomes[np.lexsort((entity, -scores))]


def ref_captured(scores, outcomes, parameter) -> float:
    top = _ranked(scores, outcomes)[: _k(parameter, len(scores))]
    return float(np.nansum(top) / np.nansum(outcomes))


def ref_lift(scores, outcomes, parameter) -> float:
    top = _ranked(scores, outcomes)[: _k(parameter, len(scores))]
    return float(np.nanmean(top) / np.nanmean(outcomes))


def ref_aucc_rows(order_outcomes: np.ndarray) -> float:
    """Row-by-row trapezoid over the gains curve of an ordering with no ties."""
    y = np.nan_to_num(order_outcomes)
    gains = np.concatenate([[0.0], np.cumsum(y) / y.sum()])
    return float(np.sum((gains[1:] + gains[:-1]) / 2) / len(y))


# --------------------------------------------------------------------------- the values


@pytest.mark.parametrize("name", ["perfect", "reversed", "noisy", "constant"])
@pytest.mark.parametrize("parameter", THRESHOLDS)
def test_lift_and_captured_match_numpy(db_pool_greenfield, models, name, parameter):
    pool, scores, outcomes = db_pool_greenfield, _scores(name), _outcomes(OUTCOME)
    for metric, ref in (("lift@", ref_lift), ("captured@", ref_captured)):
        if (
            metric == "lift@"
            and np.isnan(_ranked(scores, outcomes)[: _k(parameter, N)]).all()
        ):
            assert _stored(pool, models[name], metric, parameter)["value"] is None
            continue
        row = _stored(pool, models[name], metric, parameter)
        assert row["value"] == pytest.approx(ref(scores, outcomes, parameter), abs=1e-9)
        assert row["num_labeled"] == N_LABELED
        k = min(_k(parameter, N), N)
        expected_m = int((~np.isnan(_ranked(scores, outcomes)[:k])).sum())
        assert row["num_labeled_above_threshold"] == expected_m


def test_the_cut_ranks_the_whole_population(db_pool_greenfield, models):
    """At 100_abs every row of the population is selected, so captured is 1 and lift is 1."""
    pool = db_pool_greenfield
    for name in models:
        assert _stored(pool, models[name], "captured@", "100_abs")[
            "value"
        ] == pytest.approx(1)
        assert _stored(pool, models[name], "lift@", "100_abs")[
            "value"
        ] == pytest.approx(1)
    # 10_pct of the population is 5 rows, not 10% of the 40 labeled ones
    assert (
        _stored(pool, models["noisy"], "lift@", "10_pct")["num_labeled_above_threshold"]
        <= 5
    )


def test_aucc_is_largest_for_the_perfect_ranking(db_pool_greenfield, models):
    pool = db_pool_greenfield
    outcomes = _outcomes(OUTCOME)
    best = ref_aucc_rows(np.sort(np.nan_to_num(outcomes))[::-1])
    worst = ref_aucc_rows(np.sort(np.nan_to_num(outcomes)))
    stored = {name: _stored(pool, mid, "aucc")["value"] for name, mid in models.items()}
    assert stored["perfect"] == pytest.approx(best, abs=1e-9)
    assert stored["noisy"] == pytest.approx(
        ref_aucc_rows(_ranked(_scores("noisy"), outcomes)), abs=1e-9
    )
    assert stored["constant"] == pytest.approx(0.5, abs=1e-12)
    # reversed puts the unlabeled rows (score -1000) below the zeros: not the full worst order
    assert worst <= stored["reversed"] < 0.5 < stored["noisy"] < stored["perfect"]


def test_spearman_matches_scipy(db_pool_greenfield, models):
    pool = db_pool_greenfield
    for name in ("perfect", "reversed", "noisy"):
        expected = spearmanr(_LABELED_SCORE[name], OUTCOME)
        assert _stored(pool, models[name], "spearman")["value"] == pytest.approx(
            expected, abs=1e-9
        )
    assert _stored(pool, models["perfect"], "spearman")["value"] == pytest.approx(1)
    # a constant score has no rank order: scipy returns NaN, the SQL NULL
    assert _stored(pool, models["constant"], "spearman")["value"] is None


def test_a_signed_or_zero_target_has_no_share(db_pool_greenfield, models):
    pool = db_pool_greenfield
    for as_of in (R2, R0):
        for metric, parameter in (
            ("lift@", "100_abs"),
            ("lift@", "10_pct"),
            ("captured@", "10_pct"),
            ("aucc", ""),
        ):
            row = _stored(pool, models["noisy"], metric, parameter, as_of)
            assert row["value"] is None, (metric, parameter, as_of)
            assert row["num_labeled"] == N_LABELED
    # Spearman is a correlation: defined for a signed target, undefined for a constant one
    expected = spearmanr(_LABELED_SCORE["noisy"], SIGNED)
    assert _stored(pool, models["noisy"], "spearman", as_of=R2)[
        "value"
    ] == pytest.approx(expected, abs=1e-9)
    assert _stored(pool, models["noisy"], "spearman", as_of=R0)["value"] is None


def test_the_four_metrics_are_higher_is_better(db_pool_greenfield):
    with db_pool_greenfield.connection() as conn:
        for metric in ("lift@", "captured@", "aucc", "spearman"):
            assert (
                conn.execute(
                    "select triage.higher_is_better(%(m)s) as h", {"m": metric}
                ).fetchone()["h"]
                is True
            ), metric


# ---------------------------------------------------------------------- the gains curve


def test_the_gains_curve_agrees_with_the_cuts(db_pool_greenfield, models):
    pool = db_pool_greenfield
    model_id = models["noisy"]
    with pool.connection() as conn:
        rows = conn.execute(
            "select * from triage.gains_curve(%(m)s, 'test', %(d)s, 10)",
            {"m": model_id, "d": R1},
        ).fetchall()
    assert len(rows) == 11
    assert rows[0]["k"] == 0 and rows[0]["captured"] == 0 and rows[0]["lift"] is None
    assert rows[-1]["k"] == N and rows[-1]["captured"] == pytest.approx(1)
    assert [r["k"] for r in rows] == sorted(r["k"] for r in rows)
    tenth = rows[1]
    assert tenth["k"] == 5
    assert tenth["captured"] == pytest.approx(
        _stored(pool, model_id, "captured@", "10_pct")["value"], abs=1e-12
    )
    assert tenth["lift"] == pytest.approx(
        _stored(pool, model_id, "lift@", "10_pct")["value"], abs=1e-12
    )
    top = _ranked(_scores("noisy"), _outcomes(OUTCOME))[:5]
    assert tenth["mean_outcome_topk"] == pytest.approx(float(np.nanmean(top)))
    assert tenth["n_labeled_topk"] == int((~np.isnan(top)).sum())


def test_the_gains_curve_on_a_signed_target_and_an_unscored_date(
    db_pool_greenfield, models
):
    pool = db_pool_greenfield
    with pool.connection() as conn:
        signed = conn.execute(
            "select * from triage.gains_curve(%(m)s, 'test', %(d)s, 4)",
            {"m": models["noisy"], "d": R2},
        ).fetchall()
        missing = conn.execute(
            "select * from triage.gains_curve(%(m)s, 'test', date '1999-01-01')",
            {"m": models["noisy"]},
        ).fetchall()
    assert len(signed) == 5
    assert all(r["captured"] is None and r["lift"] is None for r in signed)
    assert signed[-1]["mean_outcome_topk"] == pytest.approx(float(SIGNED.mean()))
    assert missing == []


# ------------------------------------------------------------------- audit and migration


def test_the_audit_recomputes_the_allocation_metrics(db_pool_greenfield, models):
    report = metric_audit.audit(db_pool_greenfield, list(models.values()))
    assert report.passed, json.dumps(report.to_dict(), default=str)
    by_name = {m.metric: m for m in report.metrics}
    assert {"lift@", "captured@", "aucc", "spearman"} <= set(by_name)
    # 4 models × 3 dates × 4 thresholds
    assert by_name["lift@"].checked == 48
    assert all(m.max_gap <= 1e-9 for m in report.metrics)


def test_the_audit_catches_a_corrupted_allocation_row(db_pool_greenfield, models):
    pool = db_pool_greenfield
    with pool.connection() as conn:
        conn.execute(
            "update triage.evaluations set value = value * 1.01 where model_id = %(m)s"
            " and metric = 'aucc' and as_of_date = %(d)s",
            {"m": models["noisy"], "d": R1},
        )
    report = metric_audit.audit(pool, [models["noisy"]])
    aucc = next(m for m in report.metrics if m.metric == "aucc")
    assert aucc.failed == 1 and not report.passed


def test_0035_round_trips(db_pool_greenfield, db_url, models):
    downgrade_db(dburl=db_url, revision="0034_regression_error_trees")
    with db_pool_greenfield.connection() as conn:
        left = conn.execute(
            "select count(*) as n from triage.evaluations"
            " where metric in ('lift@', 'captured@', 'aucc', 'spearman')"
        ).fetchone()["n"]
        assert left == 0
        with pytest.raises(Exception, match="unknown regression metric"):
            conn.execute(
                "select triage.regression_metric(%(m)s, 'test', %(d)s,"
                " cast(%(ts)s as interval), 'aucc')",
                {"m": models["noisy"], "d": R1, "ts": LABEL_TIMESPAN},
            )
    upgrade_db(dburl=db_url, revision="head")
    evaluate_in_db(
        db_pool_greenfield,
        models["noisy"],
        R1,
        LABEL_TIMESPAN,
        metric_config=DEFAULT_REGRESSION_RANKING_CONFIG,
    )
    assert _stored(db_pool_greenfield, models["noisy"], "aucc")["value"] is not None
