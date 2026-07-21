"""Target-history point-in-time path (ADR-0030, Phase 3) — the leakage-boundary gate.

Two boundaries, checked by construction against a synthetic fixture with KNOWN realized dates:

* **windowed-label lags** — a label at date ``t`` with horizon ``w`` is admissible only where
  ``t + w <= as_of_date`` (its window must have elapsed to be knowable);
* **raw periodic series** — a period is admissible only where ``knowledge_date < as_of_date``.

A green run here is what gates Phase 4 (the time-series baselines that consume this history).
"""

from datetime import date

import polars as pl
import pytest

from triage.adapters.target_history import (
    HistoryQueryRequired,
    build_target_history_lags,
    build_target_history_series,
    require_history_query,
    validate_history_query,
)

TIMESPAN = "6 months"


def _seed_labels(pool, rows):
    """Insert a labels artifact + ``triage.labels`` rows — ``rows`` = (entity_id, as_of, outcome)."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('lab-art-1', 'lab-logical-1', 'labels', '{}'::jsonb) "
            "on conflict do nothing"
        )
        for entity_id, as_of, outcome in rows:
            conn.execute(
                "insert into triage.labels "
                "(label_hash, entity_id, as_of_date, label_timespan, outcome) "
                "values ('lab-art-1', %(e)s, %(d)s, %(w)s::interval, %(o)s)",
                {"e": entity_id, "d": as_of, "w": TIMESPAN, "o": outcome},
            )
    return "lab-art-1"


# --------------------------------------------------------------- windowed-label lags (a)


def test_lags_respect_point_in_time_boundary(db_pool_greenfield):
    """The current label (window open) and any prior whose window has not elapsed by
    ``as_of_date`` must be EXCLUDED from the lags — the leak test."""
    pool = db_pool_greenfield
    # With w = 6 months, a label at t is knowable at t + 6 months.
    label_hash = _seed_labels(
        pool,
        [
            (
                1,
                date(2014, 1, 1),
                10.0,
            ),  # closes 2014-07-01 -> admissible at 2015-01-01
            (
                1,
                date(2014, 7, 1),
                20.0,
            ),  # closes 2015-01-01 -> admissible (boundary <=)
            (1, date(2014, 10, 1), 25.0),  # closes 2015-04-01 -> LEAK at 2015-01-01
            (1, date(2015, 1, 1), 30.0),  # the current label -> LEAK at 2015-01-01
        ],
    )

    frame = build_target_history_lags(
        pool, label_hash, [date(2015, 1, 1)], TIMESPAN, n_lags=3
    )

    row = frame.filter(pl.col("entity_id") == 1).to_dicts()[0]
    # admissible priors, most-recent-first: 2014-07-01 (20), 2014-01-01 (10); nothing else.
    assert row["_target_lag_1"] == 20.0
    assert row["_target_lag_2"] == 10.0
    assert row["_target_lag_3"] is None
    vals = {row["_target_lag_1"], row["_target_lag_2"], row["_target_lag_3"]}
    assert 25.0 not in vals and 30.0 not in vals  # the leaks never appear


def test_lags_cold_start_earliest_date_has_no_row(db_pool_greenfield):
    """The earliest as_of_date has no admissible prior → no row (caller left-joins → NULL)."""
    pool = db_pool_greenfield
    label_hash = _seed_labels(pool, [(1, date(2014, 1, 1), 10.0)])

    frame = build_target_history_lags(
        pool, label_hash, [date(2014, 1, 1)], TIMESPAN, n_lags=2
    )

    assert frame.filter(pl.col("entity_id") == 1).height == 0


def test_lags_are_deterministic_across_runs(db_pool_greenfield):
    """Same inputs → byte-identical output (identity/caching stability)."""
    pool = db_pool_greenfield
    label_hash = _seed_labels(
        pool, [(1, date(2014, 1, 1), 10.0), (1, date(2014, 7, 1), 20.0)]
    )

    a = build_target_history_lags(pool, label_hash, [date(2015, 1, 1)], TIMESPAN, 2)
    b = build_target_history_lags(pool, label_hash, [date(2015, 1, 1)], TIMESPAN, 2)

    assert a.to_dicts() == b.to_dicts()


# ------------------------------------------------------------- raw periodic series (b)

HISTORY_QUERY = (
    "select entity_id, date_trunc('month', period)::date as period, sum(value) as value "
    "from test_events where knowledge_date < {as_of_date} group by 1, 2"
)


def _seed_events(pool, rows):
    """rows = (entity_id, knowledge_date, period, value)."""
    with pool.connection() as conn:
        conn.execute(
            "create table test_events "
            "(entity_id bigint, knowledge_date date, period date, value double precision)"
        )
        for e, k, p, v in rows:
            conn.execute(
                "insert into test_events values (%(e)s, %(k)s, %(p)s, %(v)s)",
                {"e": e, "k": k, "p": p, "v": v},
            )


def test_raw_series_excludes_future_knowledge(db_pool_greenfield):
    """No period whose ``knowledge_date >= as_of_date`` may appear in the sidecar — leak test."""
    pool = db_pool_greenfield
    _seed_events(
        pool,
        [
            (1, date(2014, 12, 1), date(2014, 12, 1), 5.0),  # known before -> included
            (1, date(2015, 1, 1), date(2015, 1, 1), 99.0),  # known AT as_of -> LEAK
            (1, date(2015, 2, 1), date(2015, 2, 1), 77.0),  # known after   -> LEAK
        ],
    )

    frame = build_target_history_series(pool, HISTORY_QUERY, [date(2015, 1, 1)])

    values = {r["value"] for r in frame.to_dicts()}
    assert values == {5.0}  # only the pre-as_of-knowledge event survives


# ------------------------------------------------------------------ config validation


def test_validate_history_query_requires_placeholder_and_single_select():
    with pytest.raises(ValueError, match="as_of_date"):
        validate_history_query("select entity_id, period, value from t")
    with pytest.raises(ValueError, match="single SELECT"):
        validate_history_query("select 1 where x < {as_of_date}; drop table t")
    validate_history_query(HISTORY_QUERY)  # valid — no raise


def test_require_history_query_enforces_raw_series_contract():
    grid = {"triage.component.catwalk.baselines.timeseries.HoltWinters": {}}
    with pytest.raises(HistoryQueryRequired, match="HoltWinters"):
        require_history_query(grid, history_query=None)
    require_history_query(grid, history_query=HISTORY_QUERY)  # satisfied
    # a grid with no raw-series baseline never requires a history_query
    require_history_query({"sklearn.dummy.DummyRegressor": {}}, history_query=None)
