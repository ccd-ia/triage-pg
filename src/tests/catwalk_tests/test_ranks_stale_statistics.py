"""The ranks functions read each label once, even with stale statistics (#102, migration 0042).

A run writes a labels artifact with a label timespan the statistics have never seen, then
appends a new model's predictions, and evaluates before autoanalyze runs. Both sides then
look like about one row. In #102 the planner answered with a nested loop that scanned the
whole labels artifact once per prediction: 96 minutes for one model. These tests rebuild
that state at small scale and leave the planner only nested loops, the join it chose there.
They count the label tuples each ranks function reads in its own transaction.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

import pytest

from triage.component.results_schema import downgrade_db, upgrade_db

AS_OF_DATE = "2014-01-01"
NEW_TIMESPAN = "3 months"
N_PRED = 2000  # predictions of the new model
N_NEW_LABELS = 2 * N_PRED  # labels of the new artifact at the new timespan
N_OLD = 60000  # analyzed history: other artifacts, a 1 month timespan, older dates

RANKS = {
    "population_ranks": "entity_id, score, rank_abs, outcome",
    "labeled_ranks": "entity_id, score, rank_abs, outcome",
    "survival_ranks": "entity_id, score, duration, event_observed",
}


def _seed_stale(pool) -> int:
    """Analyzed history, then a new labels artifact and a new model's predictions, unanalyzed."""
    with pool.connection() as conn:
        for table in ("triage.labels", "triage.predictions_default"):
            conn.execute(f"alter table {table} set (autovacuum_enabled = false)")
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " select 'old-' || i, 'old-logical-' || i, 'labels', '{}'::jsonb"
            " from generate_series(0, 39) i"
        )
        conn.execute(
            "insert into triage.labels"
            " (label_hash, entity_id, as_of_date, label_timespan, outcome)"
            " select 'old-' || (e %% 40), e,"
            "        date '2013-01-01' + (e %% 12) * interval '1 month',"
            "        interval '1 month', (e %% 2)::float"
            " from generate_series(1, %(n)s) e",
            {"n": N_OLD},
        )
        conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('mg', 'sklearn.dummy.DummyClassifier', '{}'::jsonb, ARRAY['f'])"
        )
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " ('model-old', 'model-old-logical', 'model', '{}'::jsonb),"
            " ('model-new', 'model-new-logical', 'model', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.models (model_group_id, model_hash, train_end_time)"
            " select model_group_id, h, date '2013-01-01'"
            " from triage.model_groups, unnest(array['model-old', 'model-new']) h"
        )
        old_model = conn.execute(
            "select model_id from triage.models where model_hash = 'model-old'"
        ).fetchone()["model_id"]
        conn.execute(
            "insert into triage.predictions (model_id, entity_id, as_of_date, split_kind, score)"
            " select %(m)s, e, date '2013-01-01', 'test', random()"
            " from generate_series(1, %(n)s) e",
            {"m": old_model, "n": N_OLD},
        )
    with pool.connection() as conn:
        conn.execute("analyze triage.labels")
        conn.execute("analyze triage.predictions_default")

    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " ('labels-new', 'labels-new-logical', 'labels', '{}'::jsonb),"
            " ('matrix-new', 'matrix-new-logical', 'matrix', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values ('matrix-new', 'labels-new')"
        )
        matrix_uuid = str(uuid.uuid4())
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind, storage_uri,"
            " label_timespan) values (%(u)s, 'matrix-new', 'test', 'mem://new',"
            " cast(%(ts)s as interval))",
            {"u": matrix_uuid, "ts": NEW_TIMESPAN},
        )
        conn.execute(
            "insert into triage.labels (label_hash, entity_id, as_of_date, label_timespan,"
            " outcome, duration, event_observed)"
            " select 'labels-new', e, cast(%(d)s as date), cast(%(ts)s as interval),"
            "        (e %% 2)::float, (e %% 90)::float, e %% 3 = 0"
            " from generate_series(1, %(n)s) e",
            {"d": AS_OF_DATE, "ts": NEW_TIMESPAN, "n": N_NEW_LABELS},
        )
        new_model = conn.execute(
            "select model_id from triage.models where model_hash = 'model-new'"
        ).fetchone()["model_id"]
        conn.execute(
            "insert into triage.predictions"
            " (model_id, entity_id, as_of_date, split_kind, score, matrix_uuid)"
            " select %(m)s, e, cast(%(d)s as date), 'test', (e * 7919 %% 1000) / 1000.0, %(u)s"
            " from generate_series(1, %(n)s) e",
            {"m": new_model, "d": AS_OF_DATE, "n": N_PRED, "u": matrix_uuid},
        )
    return int(new_model)


def _labels_tuples_read(conn) -> int:
    # The xact view also holds counts not yet flushed from earlier transactions on this
    # backend, so callers take a difference.
    return int(
        conn.execute(
            "select coalesce(idx_tup_fetch, 0) + coalesce(seq_tup_read, 0) as n"
            " from pg_stat_xact_user_tables where relid = 'triage.labels'::regclass"
        ).fetchone()["n"]
    )


def _read_ranks(pool, function: str, model_id: int) -> tuple[list[dict[str, Any]], int]:
    """The function's rows, and the label tuples it read, under a nested-loop-only planner."""
    with pool.connection() as conn:
        conn.execute("set local enable_hashjoin = off")
        conn.execute("set local enable_mergejoin = off")
        start = _labels_tuples_read(conn)
        rows = conn.execute(
            f"select {RANKS[function]} from triage.{function}("
            "%(m)s, 'test', cast(%(d)s as date), cast(%(ts)s as interval))",
            {"m": model_id, "d": AS_OF_DATE, "ts": NEW_TIMESPAN},
        ).fetchall()
        read = _labels_tuples_read(conn) - start
    return rows, read


@pytest.mark.parametrize("function", sorted(RANKS))
def test_ranks_read_each_label_once_with_stale_statistics(db_pool_greenfield, function):
    model_id = _seed_stale(db_pool_greenfield)

    rows, read = _read_ranks(db_pool_greenfield, function, model_id)

    assert rows, f"{function} returned no rows"
    # one index probe per prediction; the #102 plan reads N_PRED * N_NEW_LABELS
    assert read <= 2 * N_PRED, f"{function} read {read} label tuples for {N_PRED} rows"


def test_migration_0042_round_trips(db_pool_greenfield, db_url):
    """0041's join scans the artifact per prediction; 0042 returns the same rows, linearly."""
    pool = db_pool_greenfield
    model_id = _seed_stale(pool)

    downgrade_db(dburl=db_url, revision="0041_entity_attributes_scoped")
    before = {f: _read_ranks(pool, f, model_id) for f in RANKS}
    _, read_0041 = before["population_ranks"]
    assert read_0041 > 100 * N_PRED  # quadratic: the #102 plan

    upgrade_db(dburl=db_url, revision="head")
    for function, (rows_0041, _) in before.items():
        rows_0042, read = _read_ranks(pool, function, model_id)
        assert Counter(map(lambda r: tuple(r.values()), rows_0042)) == Counter(
            map(lambda r: tuple(r.values()), rows_0041)
        ), function
        assert read <= 2 * N_PRED, function
