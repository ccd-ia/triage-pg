"""Monitoring-layer tests (ADR-0006/0027, migration 0012).

The invariant under test: append-only scoring runs at different ``scored_at`` become
distinguishable, window-correct history. Drift math is cross-checked against references —
PSI against a hand-computed mirror of the ε-smoothed reference-decile formula, KS against
``scipy.stats.ks_2samp`` (the SQL ECDF aggregates ties per distinct score exactly as scipy
does). Calibration runs over the artifact-pinned ``labeled_ranks``; outcome tracking rides
the idempotent ``evaluations`` upserts.
"""

from __future__ import annotations

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
    assert row["ks"] == pytest.approx(stats.ks_2samp(ref, win).statistic, abs=1e-9)
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

    downgrade_db(dburl=db_url, revision="-1")
    with db_pool_greenfield.connection() as conn:
        assert not _has_view(conn, "triage.monitoring_volume")
        assert not _has_view(conn, "triage.monitoring_outcome_tracking")

    upgrade_db(dburl=db_url, revision="head")
    with db_pool_greenfield.connection() as conn:
        assert _has_view(conn, "triage.monitoring_volume")
