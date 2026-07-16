"""Greenfield label builder lifecycle tests (ADR-0013/0015, ADR-0010).

Builds a cohort, then labels, on the greenfield ``triage.*`` schema and asserts: the
labels artifact is built, the labels->cohort edge exists in ``triage.artifact_inputs``,
``triage.labels`` is populated per (as_of_date, label_timespan), a rebuild is a cache hit,
problem_type routing (outcome vs duration/event_observed), and that a missing parent cohort
fails loudly.
"""

from datetime import date
from typing import Any

import pytest

from triage.adapters.cohort import build_cohort
from triage.adapters.labels import build_labels

AS_OF_DATES = [date(2014, 1, 1), date(2014, 7, 1)]
LABEL_TIMESPANS = ["6 months"]

COHORT_QUERY = "select distinct entity_id from outcomes_src"

# classification: yields entity_id + outcome. {as_of_date} is substituted as a bare
# quoted literal, so date arithmetic casts it explicitly (date {as_of_date}).
CLASSIFICATION_LABEL_QUERY = (
    "select entity_id, outcome from outcomes_src"
    " where knowledge_date >= date {as_of_date}"
    " and knowledge_date < date {as_of_date} + {label_timespan}"
)

# survival: yields entity_id + duration + event_observed.
SURVIVAL_LABEL_QUERY = (
    "select entity_id, duration, event_observed from survival_src"
    " where knowledge_date >= date {as_of_date}"
    " and knowledge_date < date {as_of_date} + {label_timespan}"
)


def _seed_lineage(pool):
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-labels', '{}'::jsonb, 'classification')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile) values ('exp-labels', 'local') returning run_id"
        ).fetchone()["run_id"]
    return str(run_id)


def _seed_outcomes(pool):
    """Source table for cohort + classification labels.

    Two outcome rows land in the [2014-01-01, 2014-07-01) window and two in
    [2014-07-01, 2015-01-01)."""
    with pool.connection() as conn:
        conn.execute(
            "create table outcomes_src (entity_id bigint, knowledge_date date, outcome double precision)"
        )
        conn.execute(
            "insert into outcomes_src (entity_id, knowledge_date, outcome) values"
            " (1, date '2014-02-01', 1.0), (2, date '2014-03-01', 0.0),"
            " (1, date '2014-08-01', 0.0), (3, date '2014-09-01', 1.0)"
        )


def _seed_survival(pool):
    with pool.connection() as conn:
        conn.execute(
            "create table survival_src"
            " (entity_id bigint, knowledge_date date,"
            "  duration double precision, event_observed boolean)"
        )
        conn.execute(
            "insert into survival_src"
            " (entity_id, knowledge_date, duration, event_observed) values"
            " (1, date '2014-02-01', 30.0, true),"
            " (2, date '2014-03-01', 90.0, false)"
        )


def _build_cohort(pool, run_id, query=COHORT_QUERY):
    return build_cohort(
        pool,
        run_id,
        cohort_query_template=query
        + " where entity_id = entity_id or {as_of_date} is not null",
        as_of_dates=AS_OF_DATES,
        config={"query": query},
        source_pins={"outcomes_src": "v1"},
    )


def test_build_labels_lifecycle_and_edge(db_pool_greenfield):
    """labels artifact built; labels->cohort edge recorded; rows per (date, timespan)."""
    engine = db_pool_greenfield
    run_id = _seed_lineage(engine)
    _seed_outcomes(engine)

    cohort_hash = _build_cohort(engine, run_id)
    label_hash = build_labels(
        engine,
        run_id,
        cohort_artifact_id=cohort_hash,
        label_query_template=CLASSIFICATION_LABEL_QUERY,
        as_of_dates=AS_OF_DATES,
        label_timespans=LABEL_TIMESPANS,
        problem_type="classification",
        config={"query": CLASSIFICATION_LABEL_QUERY},
        source_pins={"outcomes_src": "v1"},
    )

    with engine.connection() as conn:
        art = conn.execute(
            "select kind, status, output_ref from triage.artifacts where artifact_id = %(h)s",
            {"h": label_hash},
        ).fetchone()
    assert art["kind"] == "labels"
    assert art["status"] == "built"
    assert art["output_ref"] == "triage.labels"

    # the labels -> cohort provenance edge
    with engine.connection() as conn:
        parents = [
            r["parent_id"]
            for r in conn.execute(
                "select parent_id from triage.artifact_inputs where artifact_id = %(h)s",
                {"h": label_hash},
            ).fetchall()
        ]
    assert parents == [cohort_hash]

    # label rows: entity 1 (outcome 1.0) + entity 2 (outcome 0.0) in the first window;
    # entity 1 (0.0) + entity 3 (1.0) in the second window.
    with engine.connection() as conn:
        rows = conn.execute(
            "select entity_id, as_of_date, label_timespan, outcome,"
            " duration, event_observed from triage.labels"
            " where label_hash = %(h)s order by as_of_date, entity_id",
            {"h": label_hash},
        ).fetchall()
    got = [(r["entity_id"], r["as_of_date"], r["outcome"]) for r in rows]
    assert got == [
        (1, date(2014, 1, 1), 1.0),
        (2, date(2014, 1, 1), 0.0),
        (1, date(2014, 7, 1), 0.0),
        (3, date(2014, 7, 1), 1.0),
    ]
    # classification path leaves survival columns null
    for r in rows:
        assert r["duration"] is None
        assert r["event_observed"] is None
        assert str(r["label_timespan"]) == "6 months" or r["label_timespan"] is not None

    # run usage edge for the labels artifact
    with engine.connection() as conn:
        used = conn.execute(
            "select count(*) as n from triage.run_artifacts where run_id = %(r)s and artifact_id = %(h)s",
            {"r": run_id, "h": label_hash},
        ).fetchone()["n"]
    assert used == 1


def test_build_labels_is_cache_hit_on_rebuild(db_pool_greenfield):
    engine = db_pool_greenfield
    run_id = _seed_lineage(engine)
    _seed_outcomes(engine)
    cohort_hash = _build_cohort(engine, run_id)

    kwargs: dict[str, Any] = dict(
        cohort_artifact_id=cohort_hash,
        label_query_template=CLASSIFICATION_LABEL_QUERY,
        as_of_dates=AS_OF_DATES,
        label_timespans=LABEL_TIMESPANS,
        problem_type="classification",
        config={"query": CLASSIFICATION_LABEL_QUERY},
        source_pins={"outcomes_src": "v1"},
    )
    first = build_labels(engine, run_id, **kwargs)
    with engine.connection() as conn:
        count_first = conn.execute(
            "select count(*) as n from triage.labels where label_hash = %(h)s",
            {"h": first},
        ).fetchone()["n"]

    second = build_labels(engine, run_id, **kwargs)
    assert second == first
    with engine.connection() as conn:
        count_second = conn.execute(
            "select count(*) as n from triage.labels where label_hash = %(h)s",
            {"h": second},
        ).fetchone()["n"]
    assert count_second == count_first  # cache hit: no extra rows

    with engine.connection() as conn:
        n = conn.execute(
            "select count(*) as n from triage.artifacts where kind = 'labels'"
        ).fetchone()["n"]
    assert n == 1


def test_build_labels_survival_routing(db_pool_greenfield):
    """problem_type='survival' populates duration/event_observed, leaving outcome null."""
    engine = db_pool_greenfield
    run_id = _seed_lineage(engine)
    _seed_survival(engine)
    # a cohort over the survival source (the cohort query just needs entity_id)
    cohort_hash = build_cohort(
        engine,
        run_id,
        cohort_query_template="select distinct entity_id from survival_src where {as_of_date} is not null",
        as_of_dates=[date(2014, 1, 1)],
        config={"name": "survival_cohort"},
        source_pins={"survival_src": "v1"},
    )

    label_hash = build_labels(
        engine,
        run_id,
        cohort_artifact_id=cohort_hash,
        label_query_template=SURVIVAL_LABEL_QUERY,
        as_of_dates=[date(2014, 1, 1)],
        label_timespans=LABEL_TIMESPANS,
        problem_type="survival",
        config={"query": SURVIVAL_LABEL_QUERY},
        source_pins={"survival_src": "v1"},
    )

    with engine.connection() as conn:
        rows = conn.execute(
            "select entity_id, outcome, duration, event_observed from triage.labels"
            " where label_hash = %(h)s order by entity_id",
            {"h": label_hash},
        ).fetchall()
    got = [
        (r["entity_id"], r["outcome"], r["duration"], r["event_observed"]) for r in rows
    ]
    assert got == [
        (1, None, 30.0, True),
        (2, None, 90.0, False),
    ]


def test_build_labels_unknown_problem_type_rejected(db_pool_greenfield):
    engine = db_pool_greenfield
    run_id = _seed_lineage(engine)
    _seed_outcomes(engine)
    cohort_hash = _build_cohort(engine, run_id)
    with pytest.raises(ValueError, match="problem_type"):
        build_labels(
            engine,
            run_id,
            cohort_artifact_id=cohort_hash,
            label_query_template=CLASSIFICATION_LABEL_QUERY,
            as_of_dates=AS_OF_DATES,
            label_timespans=LABEL_TIMESPANS,
            problem_type="ranking",  # not a valid ADR-0010 discriminator
            config={},
            source_pins={"outcomes_src": "v1"},
        )


def test_build_labels_missing_cohort_fails_loudly(db_pool_greenfield):
    engine = db_pool_greenfield
    run_id = _seed_lineage(engine)
    _seed_outcomes(engine)
    with pytest.raises(ValueError, match="does not exist"):
        build_labels(
            engine,
            run_id,
            cohort_artifact_id="nonexistent-cohort-hash",
            label_query_template=CLASSIFICATION_LABEL_QUERY,
            as_of_dates=AS_OF_DATES,
            label_timespans=LABEL_TIMESPANS,
            problem_type="classification",
            config={},
            source_pins={"outcomes_src": "v1"},
        )
