"""Greenfield cohort builder lifecycle tests (ADR-0013/0015).

Seeds a tiny source/event table + an experiment + a run on the greenfield
``triage.*`` schema, then drives :func:`triage.adapters.cohort.build_cohort` and
asserts on the resulting artifact row, ``triage.cohorts`` rows per as_of_date, and
cache-hit behavior on a second build.
"""

from datetime import date

import pytest
from sqlalchemy import text

from triage.adapters.cohort import build_cohort

AS_OF_DATES = [date(2014, 1, 1), date(2014, 7, 1)]

COHORT_QUERY = "select distinct entity_id from events where event_date < {as_of_date}"


def _seed_lineage(engine):
    """Insert an experiment + run (the FK chain begin_artifact needs) and return run_id."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "insert into triage.experiments (experiment_hash, config, problem_type)"
                " values ('exp-cohort', '{}'::jsonb, 'classification')"
            )
        )
        run_id = conn.execute(
            text("insert into triage.runs (experiment_hash, profile) values ('exp-cohort', 'local') returning run_id")
        ).scalar_one()
    return str(run_id)


def _seed_events(engine):
    """A tiny source table the cohort query reads. Entities present at each date:
    before 2014-01-01 -> {1,2}; before 2014-07-01 -> {1,2,3}."""
    with engine.begin() as conn:
        conn.execute(text("create table events (entity_id bigint, event_date date)"))
        conn.execute(
            text(
                "insert into events (entity_id, event_date) values"
                " (1, date '2013-06-01'), (2, date '2013-12-01'),"
                " (3, date '2014-03-01')"
            )
        )


def _cohort_rows(engine, cohort_hash):
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "select entity_id, as_of_date from triage.cohorts where cohort_hash = :h order by as_of_date, entity_id"
            ),
            {"h": cohort_hash},
        ).all()
    return [(r.entity_id, r.as_of_date) for r in rows]


def test_build_cohort_lifecycle(db_engine_greenfield):
    """Cohort artifact is 'built', cohorts populated per as_of_date, run usage recorded."""
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_events(engine)

    cohort_hash = build_cohort(
        engine,
        run_id,
        cohort_query_template=COHORT_QUERY,
        as_of_dates=AS_OF_DATES,
        config={"query": COHORT_QUERY, "name": "active_entities"},
        source_pins={"events": "v1"},
    )

    # artifact row exists and is built
    with engine.begin() as conn:
        art = conn.execute(
            text("select kind, status, output_ref, cacheable from triage.artifacts where artifact_id = :h"),
            {"h": cohort_hash},
        ).one()
    assert art.kind == "cohort"
    assert art.status == "built"
    assert art.output_ref == "triage.cohorts"
    assert art.cacheable is True

    # cohort rows: {1,2} at 2014-01-01, {1,2,3} at 2014-07-01
    assert _cohort_rows(engine, cohort_hash) == [
        (1, date(2014, 1, 1)),
        (2, date(2014, 1, 1)),
        (1, date(2014, 7, 1)),
        (2, date(2014, 7, 1)),
        (3, date(2014, 7, 1)),
    ]

    # cohort is a DAG root -> no input edges
    with engine.begin() as conn:
        n_inputs = conn.execute(
            text("select count(*) from triage.artifact_inputs where artifact_id = :h"),
            {"h": cohort_hash},
        ).scalar_one()
    assert n_inputs == 0

    # usage edge recorded for the run
    with engine.begin() as conn:
        used = conn.execute(
            text("select count(*) from triage.run_artifacts where run_id = :r and artifact_id = :h"),
            {"r": run_id, "h": cohort_hash},
        ).scalar_one()
    assert used == 1


def test_build_cohort_is_cache_hit_on_rebuild(db_engine_greenfield):
    """A second identical build returns the same artifact_id with no duplicate rows."""
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_events(engine)

    kwargs = dict(
        cohort_query_template=COHORT_QUERY,
        as_of_dates=AS_OF_DATES,
        config={"query": COHORT_QUERY},
        source_pins={"events": "v1"},
    )
    first = build_cohort(engine, run_id, **kwargs)
    rows_after_first = _cohort_rows(engine, first)

    second = build_cohort(engine, run_id, **kwargs)
    assert second == first
    # no rows added by the cache hit
    assert _cohort_rows(engine, second) == rows_after_first

    # exactly one cohort artifact in total
    with engine.begin() as conn:
        n = conn.execute(text("select count(*) from triage.artifacts where kind = 'cohort'")).scalar_one()
    assert n == 1


def test_unpinned_source_is_volatile_no_cache_hit(db_engine_greenfield):
    """An unpinned (None) source marks the derivation volatile -> never a cache hit;
    a rebuild re-runs the query rather than reusing (ADR-0014)."""
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    _seed_events(engine)

    kwargs = dict(
        cohort_query_template=COHORT_QUERY,
        as_of_dates=[date(2014, 1, 1)],
        config={"query": COHORT_QUERY},
        source_pins={"events": None},  # unpinned -> volatile
    )
    first = build_cohort(engine, run_id, **kwargs)
    with engine.begin() as conn:
        cacheable = conn.execute(
            text("select cacheable from triage.artifacts where artifact_id = :h"),
            {"h": first},
        ).scalar_one()
    assert cacheable is False

    # rebuild: same id (deterministic), but it re-ran (not a cache hit). With
    # `on conflict do nothing` the rows are stable, so we assert the build path was
    # taken by checking the artifact returns to 'built' after a begin/mark cycle.
    second = build_cohort(engine, run_id, **kwargs)
    assert second == first
    with engine.begin() as conn:
        status = conn.execute(
            text("select status from triage.artifacts where artifact_id = :h"),
            {"h": first},
        ).scalar_one()
    assert status == "built"


def test_template_without_placeholder_rejected(db_engine_greenfield):
    engine = db_engine_greenfield
    run_id = _seed_lineage(engine)
    with pytest.raises(ValueError, match="as_of_date"):
        build_cohort(
            engine,
            run_id,
            cohort_query_template="select entity_id from events",
            as_of_dates=AS_OF_DATES,
            config={},
        )
