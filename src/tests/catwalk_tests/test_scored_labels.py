"""Score reads label each prediction the way its evaluation did (migration 0026).

The score histogram and the dashboard's predicted list joined labels through the run's labels
artifact at ``models.training_label_timespan``. A test matrix labeled over another horizon then
showed no label at all, and a run holding two labels artifacts counted every prediction twice.
``triage.scored_labels`` resolves the label per scored date from the matrix lineage, as
``population_ranks`` does for the threshold metrics.
"""

from __future__ import annotations

import math
from collections import Counter

import pytest

from triage.component.results_schema import downgrade_db, upgrade_db
from tests.catwalk_tests.test_population_topk import (
    ENTITY_IDS,
    LABELS,
    SCORES,
    _link_run,
    _seed,
)

BINS = 10


def _histogram(pool, model_id):
    with pool.connection() as conn:
        rows = conn.execute(
            "select bin, n, n_pos from triage.model_score_histogram(%(m)s, %(b)s)",
            {"m": model_id, "b": BINS},
        ).fetchall()
    return {r["bin"]: (r["n"], r["n_pos"]) for r in rows}


def _expected_histogram():
    n, n_pos = Counter(), Counter()
    for score, label in zip(SCORES, LABELS, strict=True):
        b = math.floor(score * BINS) + 1  # width_bucket(score, 0, 1, BINS)
        n[b] += 1
        n_pos[b] += label == 1
    return {b: (n[b], n_pos[b]) for b in n}


def _training_horizon(pool, model_id, horizon):
    with pool.connection() as conn:
        conn.execute(
            "update triage.models set training_label_timespan = cast(%(h)s as interval)"
            " where model_id = %(m)s",
            {"h": horizon, "m": model_id},
        )


def test_histogram_counts_labels_at_the_scored_horizon(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _training_horizon(pool, model_id, "1 year")  # the test labels are 6 months

    assert _histogram(pool, model_id) == _expected_histogram()


def test_histogram_counts_each_prediction_once(db_pool_greenfield):
    """A second labels artifact on the run must not duplicate the rows."""
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('labels-art-2', 'labels-logical-2', 'labels', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.run_artifacts (run_id, artifact_id)"
            " select run_id, 'labels-art-2' from triage.models where model_id = %(m)s",
            {"m": model_id},
        )

    assert _histogram(pool, model_id) == _expected_histogram()


def test_scored_labels_refuses_two_label_horizons_on_one_date(db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('matrix-art-2', 'matrix-logical-2', 'matrix', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind, storage_uri,"
            " label_timespan) values (gen_random_uuid(), 'matrix-art-2', 'test', 'mem://m2',"
            " interval '1 year')"
        )
        conn.execute(
            "update triage.predictions set matrix_uuid ="
            " (select matrix_uuid from triage.matrices where artifact_id = 'matrix-art-2')"
            " where model_id = %(m)s and entity_id = 1",
            {"m": model_id},
        )
        with pytest.raises(Exception, match="label timespans"):
            conn.execute(
                "select * from triage.scored_labels(%(m)s)", {"m": model_id}
            ).fetchall()


def test_migration_0026_round_trips(db_pool_greenfield, db_url):
    """Down to 0025: no scored_labels, the training-horizon histogram. Up: the scored one."""
    pool = db_pool_greenfield
    model_id = _seed(pool, SCORES, LABELS, ENTITY_IDS)
    _link_run(pool, model_id)
    _training_horizon(pool, model_id, "1 year")

    downgrade_db(dburl=db_url, revision="0025_split_ranks_and_curve")
    with pool.connection() as conn:
        exists = conn.execute(
            "select to_regprocedure('triage.scored_labels(bigint, triage.split_kind)')"
            " is not null as e"
        ).fetchone()["e"]
    assert not exists
    assert all(n_pos == 0 for _, n_pos in _histogram(pool, model_id).values())

    upgrade_db(dburl=db_url, revision="head")
    assert _histogram(pool, model_id) == _expected_histogram()
