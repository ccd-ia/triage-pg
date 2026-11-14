"""Monitoring-layer tests (ADR-0006/0027, migration 0012).

The invariant under test: append-only scoring runs at different ``scored_at`` become
distinguishable, window-correct history. Drift math is cross-checked against references —
PSI against a hand-computed mirror of the ε-smoothed reference-decile formula, KS against
``scipy.stats.ks_2samp`` (the SQL ECDF aggregates ties per distinct score exactly as scipy
does). Calibration runs over the artifact-pinned ``labeled_ranks``; outcome tracking rides
the idempotent ``evaluations`` upserts.
"""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pytest
from scipy import stats

from triage.component.catwalk.in_pg_evaluation import evaluate_in_db

AS_OF = "2026-01-01"
TIMESPAN = "14 days"
REF_FROM, REF_TO = "2026-01-01", "2026-01-08"
WIN_FROM, WIN_TO = "2026-02-01", "2026-02-08"


# ------------------------------------------------------------------ seeding


def _seed_group_and_model(pool):
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type) "
            "values ('exp-mon', '{}'::jsonb, 'classification')"
        )
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('model-art-m', 'model-log-m', 'model', '{}'::jsonb)"
        )
        group_id = conn.execute(
            "insert into triage.model_groups "
            "(model_group_hash, model_type, hyperparameters, feature_list) "
            "values ('mg-mon', 'x.Y', '{}'::jsonb, ARRAY['f1']) "
            "returning model_group_id"
        ).fetchone()["model_group_id"]
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, train_end_time) "
            "values (%(g)s, 'model-art-m', date '2025-12-01') returning model_id",
            {"g": group_id},
        ).fetchone()["model_id"]
    return group_id, model_id


def _seed_scores(pool, model_id, scores, scored_at, *, entity_start=1):
    with pool.connection() as conn:
        for offset, score in enumerate(scores):
            conn.execute(
                "insert into triage.predictions "
                "(model_id, entity_id, as_of_date, split_kind, scored_at, score) "
                "values (%(m)s, %(e)s, %(d)s, 'production', %(t)s, %(s)s)",
                {
                    "m": model_id,
                    "e": entity_start + offset,
                    "d": AS_OF,
                    "t": scored_at,
                    "s": float(score),
                },
            )


def _sql_drift(pool, group_id):
    with pool.connection() as conn:
        return conn.execute(
            "select * from triage.monitoring_score_drift(%(g)s,"
            " %(rf)s, %(rt)s, %(wf)s, %(wt)s)",
            {
                "g": group_id,
                "rf": REF_FROM,
                "rt": REF_TO,
                "wf": WIN_FROM,
                "wt": WIN_TO,
            },
        ).fetchone()


def _psi_reference(ref: np.ndarray, win: np.ndarray) -> float:
    """The exact ε-smoothed reference-decile PSI the SQL computes (see migration 0012)."""
    edges = np.percentile(ref, [10, 20, 30, 40, 50, 60, 70, 80, 90])

    def proportions(x: np.ndarray) -> np.ndarray:
        bins = np.searchsorted(edges, x, side="right")  # == width_bucket semantics
        counts = np.bincount(bins, minlength=10).astype(float)
        return counts / counts.sum()

    p_ref = proportions(ref) + 1e-6
    p_win = proportions(win) + 1e-6
    return float(np.sum((p_win - p_ref) * np.log(p_win / p_ref)))


# ------------------------------------------------------------------ drift


def test_score_drift_matches_scipy_and_psi_reference(db_pool_greenfield):
    rng = np.random.default_rng(11)
    ref = rng.beta(2, 5, 400)
    win = rng.beta(5, 2, 300)  # a real shift

    group_id, model_id = _seed_group_and_model(db_pool_greenfield)
    _seed_scores(db_pool_greenfield, model_id, ref, "2026-01-02T12:00:00+00")
    _seed_scores(
        db_pool_greenfield, model_id, win, "2026-02-02T12:00:00+00", entity_start=1001
    )

    row = _sql_drift(db_pool_greenfield, group_id)
    assert row["n_reference"] == 400 and row["n_window"] == 300
    assert row["psi"] == pytest.approx(_psi_reference(ref, win), abs=1e-9)
    # scipy-stubs types the ks_2samp result as an anonymous class lacking .statistic
    ks_stat = cast(Any, stats.ks_2samp(ref, win)).statistic
    assert row["ks"] == pytest.approx(ks_stat, abs=1e-9)
    assert row["psi"] > 0.25  # the shift is real — sanity on the rule-of-thumb scale


def test_score_drift_is_zero_against_itself(db_pool_greenfield):
    rng = np.random.default_rng(3)
    scores = rng.random(200)
    group_id, model_id = _seed_group_and_model(db_pool_greenfield)
    _seed_scores(db_pool_greenfield, model_id, scores, "2026-01-02T12:00:00+00")
    # the SAME rows appended again in the later window (identical distribution)
    _seed_scores(
        db_pool_greenfield,
        model_id,
        scores,
        "2026-02-02T12:00:00+00",
        entity_start=1001,
    )
    row = _sql_drift(db_pool_greenfield, group_id)
    assert row["psi"] == pytest.approx(0.0, abs=1e-12)
    assert row["ks"] == pytest.approx(0.0, abs=1e-12)


# ------------------------------------------------------------------ volume + append-only


def test_volume_heartbeat_and_append_only(db_pool_greenfield):
    group_id, model_id = _seed_group_and_model(db_pool_greenfield)
    _seed_scores(
        db_pool_greenfield, model_id, [0.1, 0.2, 0.3], "2026-01-02T12:00:00+00"
    )
    # a SECOND scoring run for the SAME entities at a later scored_at — appends, never mutates
    _seed_scores(
        db_pool_greenfield, model_id, [0.4, 0.5, 0.6], "2026-02-02T12:00:00+00"
    )

    with db_pool_greenfield.connection() as conn:
        volume = conn.execute(
            "select scored_on, n_predictions, n_entities from triage.monitoring_volume "
            "where model_group_id = %(g)s order by scored_on",
            {"g": group_id},
        ).fetchall()
        total = conn.execute(
            "select count(*) as n from triage.predictions where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()["n"]
        first_window = conn.execute(
            "select array_agg(score order by entity_id) as scores from triage.predictions "
            "where model_id = %(m)s and scored_at < '2026-01-08'",
            {"m": model_id},
        ).fetchone()["scores"]

    assert [(v["n_predictions"], v["n_entities"]) for v in volume] == [(3, 3), (3, 3)]
    assert total == 6  # append-only: both runs' rows coexist
    assert first_window == [0.1, 0.2, 0.3]  # the first run's rows are untouched


# ------------------------------------------------------------------ calibration


def test_calibration_deciles(db_pool_greenfield):
    _, model_id = _seed_group_and_model(db_pool_greenfield)
    scores = np.linspace(0.99, 0.0, 100)
    _seed_scores(db_pool_greenfield, model_id, scores, "2026-01-02T12:00:00+00")
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('labels-art-m', 'labels-log-m', 'labels', '{}'::jsonb)"
        )
        for entity_id in range(1, 101):
            conn.execute(
                "insert into triage.labels (label_hash, entity_id, as_of_date,"
                " label_timespan, outcome) values ('labels-art-m', %(e)s, %(d)s,"
                " cast(%(ts)s as interval), %(o)s)",
                {
                    "e": entity_id,
                    "d": AS_OF,
                    "ts": TIMESPAN,
                    # the top-scored half (entities 1-50) realized the outcome
                    "o": 1.0 if entity_id <= 50 else 0.0,
                },
            )
        rows = conn.execute(
            "select * from triage.monitoring_calibration(%(m)s, 'production',"
            " %(d)s::date, cast(%(ts)s as interval)) order by decile",
            {"m": model_id, "d": AS_OF, "ts": TIMESPAN},
        ).fetchall()

    assert len(rows) == 10 and all(r["n"] == 10 for r in rows)
    assert all(r["realized_rate"] == pytest.approx(1.0) for r in rows[:5])
    assert all(r["realized_rate"] == pytest.approx(0.0) for r in rows[5:])
    # scores fall with decile — the ranking orientation held
    assert rows[0]["avg_score"] > rows[-1]["avg_score"]
    # a model with no run lineage reads as a classifier; the base rate is 0.5
    assert all(r["mean_outcome"] == r["realized_rate"] for r in rows)
    assert [r["lift"] for r in rows] == pytest.approx([2.0] * 5 + [0.0] * 5)
    assert all(r["n_distinct_scores"] == 10 for r in rows)


def _seed_labels(pool, outcomes):
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('labels-art-m', 'labels-log-m', 'labels', '{}'::jsonb)"
        )
        for entity_id, outcome in enumerate(outcomes, start=1):
            conn.execute(
                "insert into triage.labels (label_hash, entity_id, as_of_date,"
                " label_timespan, outcome) values ('labels-art-m', %(e)s, %(d)s,"
                " cast(%(ts)s as interval), %(o)s)",
                {"e": entity_id, "d": AS_OF, "ts": TIMESPAN, "o": float(outcome)},
            )


def _as_problem_type(pool, model_id, problem_type):
    """Give the hand-seeded model a run of an experiment of ``problem_type``."""
    with pool.connection() as conn:
        conn.execute(
            "update triage.experiments set problem_type = %(t)s"
            " where experiment_hash = 'exp-mon'",
            {"t": problem_type},
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile)"
            " values ('exp-mon', 'local') returning run_id"
        ).fetchone()["run_id"]
        conn.execute(
            "update triage.models set run_id = %(r)s where model_id = %(m)s",
            {"r": run_id, "m": model_id},
        )


def _calibration(pool, model_id, bins=10):
    with pool.connection() as conn:
        return conn.execute(
            "select * from triage.monitoring_calibration(%(m)s, 'production',"
            " %(d)s::date, cast(%(ts)s as interval), '', %(b)s) order by decile",
            {"m": model_id, "d": AS_OF, "ts": TIMESPAN, "b": bins},
        ).fetchall()


def test_regression_calibration_reports_the_mean_outcome(db_pool_greenfield):
    _, model_id = _seed_group_and_model(db_pool_greenfield)
    _as_problem_type(db_pool_greenfield, model_id, "regression")
    rng = np.random.default_rng(3)
    outcomes = rng.gamma(2.0, 400.0, 100).round(1)
    scores = np.linspace(2700.0, 10.0, 100)  # entity 1 scores highest
    _seed_scores(db_pool_greenfield, model_id, scores, "2026-01-02T12:00:00+00")
    _seed_labels(db_pool_greenfield, outcomes)

    rows = _calibration(db_pool_greenfield, model_id)

    assert [r["decile"] for r in rows] == list(range(1, 11))
    overall = outcomes.mean()
    for r in rows:
        block = outcomes[(r["decile"] - 1) * 10 : r["decile"] * 10]
        assert r["n"] == 10
        assert r["mean_outcome"] == pytest.approx(block.mean())
        assert r["lift"] == pytest.approx(block.mean() / overall)
        assert r["realized_rate"] is None  # "share above zero" is not a regression rate


def test_tied_scores_share_one_decile(db_pool_greenfield):
    """84 of 100 rows share a score: ntile spread them over 8 deciles, cume_dist keeps one."""
    _, model_id = _seed_group_and_model(db_pool_greenfield)
    _as_problem_type(db_pool_greenfield, model_id, "regression")
    scores = (
        [100.0 - i for i in range(8)] + [15.9] * 84 + [5.0 - i * 0.5 for i in range(8)]
    )
    _seed_scores(db_pool_greenfield, model_id, scores, "2026-01-02T12:00:00+00")
    _seed_labels(db_pool_greenfield, [float(i % 7) for i in range(100)])

    rows = _calibration(db_pool_greenfield, model_id)
    by_decile = {r["decile"]: r for r in rows}
    # rows 1-8: deciles 1 (rows 1-8 have c ≤ 8 → ceil(c/10) = 1); the block's last row is
    # row 92, so all 84 tied rows land in decile 10 with the 8 rows below them
    assert sorted(by_decile) == [1, 10]
    assert by_decile[1]["n"] == 8 and by_decile[1]["n_distinct_scores"] == 8
    assert by_decile[10]["n"] == 92 and by_decile[10]["n_distinct_scores"] == 9
    assert sum(r["n"] for r in rows) == 100

    with db_pool_greenfield.connection() as conn:
        ties = conn.execute(
            "select * from triage.score_ties(%(m)s, 'production', %(d)s::date)",
            {"m": model_id, "d": AS_OF},
        ).fetchone()
    assert ties["n"] == 100 and ties["n_distinct"] == 17
    assert ties["largest_tie_n"] == 84
    assert ties["largest_tie_score"] == pytest.approx(15.9)
    assert ties["largest_tie_share"] == pytest.approx(0.84)


def test_score_ties_on_a_date_with_no_scores(db_pool_greenfield):
    _, model_id = _seed_group_and_model(db_pool_greenfield)
    with db_pool_greenfield.connection() as conn:
        ties = conn.execute(
            "select * from triage.score_ties(%(m)s, 'production', %(d)s::date)",
            {"m": model_id, "d": AS_OF},
        ).fetchone()
    assert (ties["n"], ties["n_distinct"]) == (0, 0)
    assert ties["largest_tie_n"] is None and ties["largest_tie_share"] is None


def test_calibration_refuses_zero_bins(db_pool_greenfield):
    _, model_id = _seed_group_and_model(db_pool_greenfield)
    with pytest.raises(Exception, match="bins must be 1 or more"):
        _calibration(db_pool_greenfield, model_id, bins=0)


# ------------------------------------------------------------------ outcome tracking


def test_outcome_tracking_sequences_realized_evaluations(db_pool_greenfield):
    group_id, model_id = _seed_group_and_model(db_pool_greenfield)
    scores = np.linspace(0.99, 0.0, 100)
    _seed_scores(db_pool_greenfield, model_id, scores, "2026-01-02T12:00:00+00")
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('labels-art-m', 'labels-log-m', 'labels', '{}'::jsonb)"
        )
        for entity_id in range(1, 101):
            conn.execute(
                "insert into triage.labels (label_hash, entity_id, as_of_date,"
                " label_timespan, outcome) values ('labels-art-m', %(e)s, %(d)s,"
                " cast(%(ts)s as interval), %(o)s)",
                {
                    "e": entity_id,
                    "d": AS_OF,
                    "ts": TIMESPAN,
                    "o": float(entity_id <= 50),
                },
            )

    # "labels arrived" → re-evaluate; the upsert writes the REALIZED metric rows
    written = evaluate_in_db(
        db_pool_greenfield,
        model_id,
        AS_OF,
        TIMESPAN,
        split_kind="production",
        metric_config={"metrics": ["precision@"], "thresholds": ["10_abs"]},
    )
    assert written == 1

    with db_pool_greenfield.connection() as conn:
        rows = conn.execute(
            "select * from triage.monitoring_outcome_tracking "
            "where model_group_id = %(g)s and metric = 'precision@'",
            {"g": group_id},
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(
        1.0
    )  # the top-10 are all realized positives
    assert rows[0]["as_of_date"].isoformat() == AS_OF
    assert (
        rows[0]["purpose"] is None
    )  # seeded model has no owning run — LEFT JOIN keeps it


# ------------------------------------------------------------------ migration roundtrip


def test_migration_0012_roundtrip(db_url, db_pool_greenfield):
    from triage.component.results_schema import downgrade_db, upgrade_db

    def _has_view(conn, name):
        return conn.execute(
            "select to_regclass(%(n)s) is not null as f", {"n": name}
        ).fetchone()["f"]

    with db_pool_greenfield.connection() as conn:
        assert _has_view(conn, "triage.monitoring_volume")
        assert _has_view(conn, "triage.monitoring_outcome_tracking")

    # explicit target (not "-1"): head moves as migrations land; the intent is
    # "below 0012" regardless of what sits above it.
    downgrade_db(dburl=db_url, revision="0011_survival_c_index")
    with db_pool_greenfield.connection() as conn:
        assert not _has_view(conn, "triage.monitoring_volume")
        assert not _has_view(conn, "triage.monitoring_outcome_tracking")

    upgrade_db(dburl=db_url, revision="head")
    with db_pool_greenfield.connection() as conn:
        assert _has_view(conn, "triage.monitoring_volume")


def test_migration_0029_round_trips(db_pool_greenfield, db_url):
    """Down to 0028: the 0012 calibration (realized rate only) and no score_ties. Up: 0029."""
    from triage.component.results_schema import downgrade_db, upgrade_db

    _, model_id = _seed_group_and_model(db_pool_greenfield)
    _seed_scores(
        db_pool_greenfield,
        model_id,
        np.linspace(0.99, 0.0, 100),
        "2026-01-02T12:00:00+00",
    )
    _seed_labels(db_pool_greenfield, [float(e <= 50) for e in range(1, 101)])

    def columns() -> list[str]:
        with db_pool_greenfield.connection() as conn:
            return list(
                conn.execute(
                    "select * from triage.monitoring_calibration(%(m)s, 'production',"
                    " %(d)s::date, cast(%(ts)s as interval)) limit 1",
                    {"m": model_id, "d": AS_OF, "ts": TIMESPAN},
                ).fetchone()
            )

    def has(signature: str) -> bool:
        with db_pool_greenfield.connection() as conn:
            return conn.execute(
                "select to_regprocedure(%(s)s) is not null as e", {"s": signature}
            ).fetchone()["e"]

    downgrade_db(dburl=db_url, revision="0028_subset_threshold_curve")
    assert columns() == ["decile", "n", "avg_score", "realized_rate"]
    assert not has("triage.score_ties(bigint, triage.split_kind, date)")
    assert not has("triage.model_problem_type(bigint)")

    upgrade_db(dburl=db_url, revision="head")
    assert columns() == [
        "decile",
        "n",
        "avg_score",
        "mean_outcome",
        "lift",
        "realized_rate",
        "n_distinct_scores",
    ]
    assert has("triage.score_ties(bigint, triage.split_kind, date)")
