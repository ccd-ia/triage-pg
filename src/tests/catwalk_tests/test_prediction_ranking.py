"""Validation of append-only predictions + SQL window-function ranking
(ADR-0006, ADR-0010).

``triage.component.catwalk.prediction_ranking`` writes append-only score rows
into the greenfield ``triage.predictions`` table and reads deterministic ranks
back from the ``triage.prediction_ranks`` view (both created by the
``0001_initial_triage_schema`` migration). This test is the proof that:

* writing is append-only — re-scoring the same (model, entity, as_of_date)
  *adds* a row with a later ``scored_at`` and never overwrites;
* ``latest_predictions`` collapses that history to the newest score;
* ``prediction_ranks`` ranks the latest scores deterministically by
  ``score desc, entity_id`` (``rank_abs`` = row_number, ``rank_pct`` =
  percent_rank), with ties broken on entity_id and NO random sort seed.
"""

import pytest

from triage.component.catwalk.prediction_ranking import (
    fetch_ranks,
    rank_predictions,
    record_predictions,
)

AS_OF_DATE = "2014-01-01"


@pytest.fixture
def greenfield_engine(db_pool_greenfield):
    """Fresh pytest-postgresql DB with the greenfield ``triage`` schema applied
    via alembic (0001 -> head), as a psycopg3 pool."""
    return db_pool_greenfield


def _seed_model(pool):
    """Insert the lineage rows a prediction needs (artifact -> model_group ->
    model), returning the new model_id. Mirrors the 0001 FK chain
    (predictions.model_id -> models.model_id, models.model_hash ->
    artifacts.artifact_id)."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('model-art-1', 'model-logical-1', 'model', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.model_groups "
            "(model_group_hash, model_type, hyperparameters, feature_list) "
            "values ('mg1', 'sklearn.tree.DecisionTreeClassifier', '{}'::jsonb, "
            "ARRAY['f1','f2'])"
        )
        model_id = conn.execute(
            "insert into triage.models "
            "(model_group_id, model_hash, train_end_time) "
            "select model_group_id, 'model-art-1', date '2013-07-01' "
            "from triage.model_groups where model_group_hash = 'mg1' "
            "returning model_id"
        ).fetchone()["model_id"]
    return model_id


def _row_count(pool, model_id):
    with pool.connection() as conn:
        return conn.execute(
            "select count(*) as n from triage.predictions where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()["n"]


# --------------------------------------------------------------- append-only write


def test_record_predictions_inserts_rows(greenfield_engine):
    """A single record_predictions call lands one row per score."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    scores = [
        {"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.9},
        {"entity_id": 2, "as_of_date": AS_OF_DATE, "score": 0.5},
        {"entity_id": 3, "as_of_date": AS_OF_DATE, "score": 0.7},
    ]
    n = record_predictions(engine, model_id, "test", scores)
    assert n == 3
    assert _row_count(engine, model_id) == 3


def test_record_predictions_is_append_only(greenfield_engine):
    """Re-scoring the SAME (model, entity, as_of_date) APPENDS rows; it never
    overwrites. The history accumulates and the new row has a later
    scored_at (ADR-0006)."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    first = [{"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.10}]
    second = [{"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.90}]

    record_predictions(engine, model_id, "test", first)
    record_predictions(engine, model_id, "test", second)

    # Two physical rows for the same key -> append-only, not upsert.
    assert _row_count(engine, model_id) == 2

    with engine.connection() as conn:
        rows = conn.execute(
            "select score, scored_at from triage.predictions "
            "where model_id = %(m)s and entity_id = 1 and as_of_date = %(d)s "
            "order by scored_at",
            {"m": model_id, "d": AS_OF_DATE},
        ).fetchall()
    assert [float(r["score"]) for r in rows] == [0.10, 0.90]
    # the appended row is strictly newer (or equal — never older)
    assert rows[1]["scored_at"] >= rows[0]["scored_at"]


def test_latest_predictions_collapses_history(greenfield_engine):
    """triage.latest_predictions picks the newest score per (model, entity,
    as_of_date), so fetch_ranks ranks the LATEST score, not the original."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    # entity 1 first low, then high; entity 2 first high, then low.
    record_predictions(
        engine,
        model_id,
        "test",
        [
            {"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.10},
            {"entity_id": 2, "as_of_date": AS_OF_DATE, "score": 0.90},
        ],
    )
    record_predictions(
        engine,
        model_id,
        "test",
        [
            {"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.95},
            {"entity_id": 2, "as_of_date": AS_OF_DATE, "score": 0.20},
        ],
    )

    ranks = fetch_ranks(engine, model_id, AS_OF_DATE)
    by_entity = {r["entity_id"]: r for r in ranks}
    # latest scores: e1=0.95, e2=0.20 -> e1 ranks first.
    assert float(by_entity[1]["score"]) == pytest.approx(0.95)
    assert float(by_entity[2]["score"]) == pytest.approx(0.20)
    assert by_entity[1]["rank_abs"] == 1
    assert by_entity[2]["rank_abs"] == 2


def test_record_predictions_empty_is_noop(greenfield_engine):
    engine = greenfield_engine
    model_id = _seed_model(engine)
    assert record_predictions(engine, model_id, "test", []) == 0
    assert _row_count(engine, model_id) == 0


def test_record_predictions_rejects_bad_split_kind(greenfield_engine):
    engine = greenfield_engine
    model_id = _seed_model(engine)
    with pytest.raises(ValueError, match="split_kind"):
        record_predictions(
            engine,
            model_id,
            "not_a_split",
            [{"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.5}],
        )


# ------------------------------------------------------------- deterministic ranks


def test_ranks_are_deterministic_by_score_desc(greenfield_engine):
    """rank_abs follows score descending; rank_pct is percent_rank."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    # scores out of order on purpose
    scores = [
        {"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.5},
        {"entity_id": 2, "as_of_date": AS_OF_DATE, "score": 0.9},
        {"entity_id": 3, "as_of_date": AS_OF_DATE, "score": 0.1},
        {"entity_id": 4, "as_of_date": AS_OF_DATE, "score": 0.7},
    ]
    ranks = rank_predictions(engine, model_id, "test", scores, AS_OF_DATE)

    # returned ordered by rank_abs -> by score desc: e2(.9), e4(.7), e1(.5), e3(.1)
    assert [r["entity_id"] for r in ranks] == [2, 4, 1, 3]
    assert [r["rank_abs"] for r in ranks] == [1, 2, 3, 4]
    # percent_rank() = (rank-1)/(n-1): 0, 1/3, 2/3, 1
    assert [pytest.approx(r["rank_pct"]) for r in ranks] == [
        pytest.approx(0.0),
        pytest.approx(1 / 3),
        pytest.approx(2 / 3),
        pytest.approx(1.0),
    ]


def test_score_ties_break_on_entity_id(greenfield_engine):
    """Within a score tie, the lower entity_id ranks first — deterministic, no
    random seed (schema-design §8.3)."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    # entities 5, 2, 8 all tie at 0.80; entity 1 is strictly highest.
    scores = [
        {"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.95},
        {"entity_id": 5, "as_of_date": AS_OF_DATE, "score": 0.80},
        {"entity_id": 2, "as_of_date": AS_OF_DATE, "score": 0.80},
        {"entity_id": 8, "as_of_date": AS_OF_DATE, "score": 0.80},
    ]
    ranks = rank_predictions(engine, model_id, "test", scores, AS_OF_DATE)
    # tie group ordered by entity_id asc: 2, 5, 8 after the leader 1.
    assert [r["entity_id"] for r in ranks] == [1, 2, 5, 8]
    assert [r["rank_abs"] for r in ranks] == [1, 2, 3, 4]


def test_ranks_partitioned_by_as_of_date(greenfield_engine):
    """Ranking partitions by (model_id, as_of_date): each date ranks
    independently from rank_abs = 1."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    other_date = "2014-07-01"
    record_predictions(
        engine,
        model_id,
        "test",
        [
            {"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.3},
            {"entity_id": 2, "as_of_date": AS_OF_DATE, "score": 0.6},
            {"entity_id": 1, "as_of_date": other_date, "score": 0.9},
            {"entity_id": 2, "as_of_date": other_date, "score": 0.1},
        ],
    )
    first = fetch_ranks(engine, model_id, AS_OF_DATE)
    second = fetch_ranks(engine, model_id, other_date)

    # each date starts ranking from 1 independently
    assert {r["entity_id"]: r["rank_abs"] for r in first} == {2: 1, 1: 2}
    assert {r["entity_id"]: r["rank_abs"] for r in second} == {1: 1, 2: 2}


def test_single_row_percent_rank_is_zero(greenfield_engine):
    """percent_rank() of a lone row is 0.0 (no division by n-1=0)."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    ranks = rank_predictions(
        engine,
        model_id,
        "test",
        [{"entity_id": 1, "as_of_date": AS_OF_DATE, "score": 0.42}],
        AS_OF_DATE,
    )
    assert len(ranks) == 1
    assert ranks[0]["rank_abs"] == 1
    assert ranks[0]["rank_pct"] == pytest.approx(0.0)
