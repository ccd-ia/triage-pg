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
    HistoryGapsError,
    HistoryPeriodMisaligned,
    HistoryQueryRequired,
    HistorySeriesError,
    TargetHistory,
    build_target_history_lags,
    build_target_history_series,
    check_history_gaps,
    pivot_series_to_history_columns,
    require_history_query,
    resolve_target_history,
    validate_history_query,
)

TIMESPAN = "6 months"
TS = "triage.component.catwalk.baselines.timeseries"


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


def test_lags_follow_the_label_date_grid_not_the_entity_rank(db_pool_greenfield):
    """An entity missing at one grid date gets NULL in that lag; its older label stays at
    its own lag instead of sliding forward (the rank-packing defect)."""
    pool = db_pool_greenfield
    label_hash = _seed_labels(
        pool,
        [
            # entity 2 is labeled at every date and defines the grid
            (2, date(2013, 1, 1), 1.0),
            (2, date(2013, 7, 1), 1.0),
            (2, date(2014, 1, 1), 1.0),
            (2, date(2014, 7, 1), 1.0),
            # entity 1 has no label at 2014-01-01, the second grid date
            (1, date(2013, 1, 1), 10.0),
            (1, date(2013, 7, 1), 20.0),
            (1, date(2014, 7, 1), 40.0),
        ],
    )

    frame = build_target_history_lags(
        pool, label_hash, [date(2015, 1, 1)], TIMESPAN, n_lags=4
    )

    row = frame.filter(pl.col("entity_id") == 1).to_dicts()[0]
    assert row["_target_lag_1"] == 40.0  # 2014-07-01
    assert row["_target_lag_2"] is None  # 2014-01-01: no label, not shifted
    assert row["_target_lag_3"] == 20.0  # 2013-07-01, three grid dates back
    assert row["_target_lag_4"] == 10.0


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


def _series(rows, as_of=date(2015, 1, 1)):
    """rows = (entity_id, period, value) at one as_of_date."""
    return pl.DataFrame(
        {
            "entity_id": [r[0] for r in rows],
            "as_of_date": [as_of] * len(rows),
            "period": [r[1] for r in rows],
            "value": [r[2] for r in rows],
        },
        schema={
            "entity_id": pl.Int64,
            "as_of_date": pl.Date,
            "period": pl.Date,
            "value": pl.Float64,
        },
    )


def test_pivot_places_each_value_at_its_calendar_offset():
    """The newest period sits in the highest slot; a series that starts late leaves its older
    slots NULL (leading), with no gap counted."""
    series = _series(
        [
            (1, date(2014, 10, 1), 10.0),
            (1, date(2014, 11, 1), 20.0),
            (1, date(2014, 12, 1), 30.0),
            (2, date(2014, 11, 1), 5.0),
            (2, date(2014, 12, 1), 6.0),
        ]
    )

    result = pivot_series_to_history_columns(series, width=4, period="1 month")

    row1 = result.frame.filter(pl.col("entity_id") == 1).to_dicts()[0]
    assert row1["_hist_3"] == 30.0  # Dec, 1 month back
    assert row1["_hist_2"] == 20.0  # Nov
    assert row1["_hist_1"] == 10.0  # Oct
    assert row1["_hist_0"] is None  # Sep: before the series starts (leading)
    row2 = result.frame.filter(pl.col("entity_id") == 2).to_dicts()[0]
    assert (row2["_hist_3"], row2["_hist_2"], row2["_hist_1"]) == (6.0, 5.0, None)
    assert result.n_series == 2
    assert result.n_series_with_gaps == 0


def test_missing_february_keeps_january_at_offset_five():
    """The meio defect: with no February row, rank packing slid January into February's slot.
    On the calendar, January stays 5 months before a June as_of_date and February is a gap."""
    as_of = date(2015, 6, 1)
    series = _series(
        [
            (1, date(2015, 1, 1), 1.0),
            (1, date(2015, 3, 1), 3.0),
            (1, date(2015, 4, 1), 4.0),
            (1, date(2015, 5, 1), 5.0),
        ],
        as_of=as_of,
    )
    width = 6

    strict = pivot_series_to_history_columns(series, width=width, period="1 month")
    row = strict.frame.to_dicts()[0]
    assert row[f"_hist_{width - 5}"] == 1.0  # January, 5 months back
    assert row[f"_hist_{width - 4}"] is None  # February, the interior gap
    assert row[f"_hist_{width - 1}"] == 5.0  # May, 1 month back
    assert row[f"_hist_{width - 6}"] is None  # December 2014: leading, not a gap
    assert strict.n_series_with_gaps == 1
    assert strict.example == {
        "entity_id": 1,
        "as_of_date": "2015-06-01",
        "first_missing_period": "2015-02-01",
    }

    null = pivot_series_to_history_columns(series, width, "1 month", fill="null")
    assert null.frame.to_dicts()[0][f"_hist_{width - 4}"] is None

    zero = pivot_series_to_history_columns(series, width, "1 month", fill="zero")
    zrow = zero.frame.to_dicts()[0]
    assert zrow[f"_hist_{width - 4}"] == 0.0  # February filled
    assert zrow[f"_hist_{width - 6}"] is None  # leading slot is never filled


def test_missing_newest_month_is_an_interior_gap():
    """Offset 1 with no row (December, for a January as_of_date) is a gap, not a late start."""
    series = _series([(1, date(2014, 10, 1), 1.0), (1, date(2014, 11, 1), 2.0)])

    result = pivot_series_to_history_columns(series, width=3, period="1 month")

    assert result.frame.to_dicts()[0]["_hist_2"] is None
    assert result.n_series_with_gaps == 1
    assert result.example is not None
    assert result.example["first_missing_period"] == "2014-12-01"


def test_period_off_the_grid_raises_misaligned():
    """A period one day off the history_period grid names the nearest grid date."""
    series = _series([(1, date(2014, 12, 2), 1.0)])

    with pytest.raises(
        HistoryPeriodMisaligned, match="nearest grid date is 2014-12-01"
    ):
        pivot_series_to_history_columns(series, width=3, period="1 month")


def test_period_at_the_as_of_date_raises_as_a_leak():
    series = _series([(1, date(2015, 1, 1), 1.0)])

    with pytest.raises(HistoryPeriodMisaligned, match="at or after their as_of_date"):
        pivot_series_to_history_columns(series, width=3, period="1 month")


def test_rows_older_than_the_window_are_dropped():
    series = _series([(1, date(2013, 1, 1), 9.0), (1, date(2014, 12, 1), 1.0)])

    result = pivot_series_to_history_columns(series, width=2, period="1 month")

    assert result.frame.to_dicts()[0] == {
        "entity_id": 1,
        "as_of_date": date(2015, 1, 1),
        "_hist_0": None,
        "_hist_1": 1.0,
    }
    assert result.n_series_with_gaps == 0


def test_duplicate_periods_raise():
    series = _series([(1, date(2014, 12, 1), 1.0), (1, date(2014, 12, 1), 2.0)])

    with pytest.raises(HistorySeriesError, match="duplicate"):
        pivot_series_to_history_columns(series, width=2, period="1 month")


def test_strict_gap_check_names_counts_and_both_fixes():
    series = _series(
        [
            (1, date(2014, 10, 1), 1.0),
            (1, date(2014, 12, 1), 2.0),
            (2, date(2014, 12, 1), 3.0),
        ]
    )
    result = pivot_series_to_history_columns(series, width=3, period="1 month")
    strict = TargetHistory(
        history_query="q",
        series_width=3,
        history_period="1 month",
        complete_calendar_baselines=(f"{TS}.SeasonalNaive",),
    )

    with pytest.raises(HistoryGapsError) as exc:
        check_history_gaps(strict, result.summary())
    message = str(exc.value)
    assert "1 of 2 target-history series" in message
    assert "period 2014-11-01" in message
    assert "SeasonalNaive" in message
    assert "fill_missing_periods: zero" in message and "generate_series" in message

    # filled, or no complete-calendar baseline in the grid -> no error
    check_history_gaps(
        TargetHistory(history_query="q", fill_missing_periods="null"), result.summary()
    )
    check_history_gaps(TargetHistory(history_query="q"), result.summary())


def test_identity_tags_the_calendar_layout_only_with_history():
    assert TargetHistory().identity() == {
        "target_history_lags": 0,
        "history_query": None,
        "history_series_width": 0,
    }
    zero = TargetHistory(
        history_query="q", history_period="1 month", fill_missing_periods="zero"
    )
    null = TargetHistory(
        history_query="q", history_period="1 month", fill_missing_periods="null"
    )
    assert zero.identity()["history_layout"] == "calendar"
    assert zero.identity() != null.identity()


def test_resolve_requires_history_period_with_a_history_query():
    grid = {f"{TS}.SeasonalNaive": {}}
    with pytest.raises(ValueError, match="history_period is required"):
        resolve_target_history(grid, {"history_query": HISTORY_QUERY})
    with pytest.raises(ValueError, match="fill_missing_periods must be"):
        resolve_target_history(
            grid,
            {
                "history_query": HISTORY_QUERY,
                "history_period": "1 month",
                "fill_missing_periods": "mean",
            },
        )
    with pytest.raises(ValueError, match="no history_query to fill"):
        resolve_target_history({}, {"fill_missing_periods": "zero"})

    resolved = resolve_target_history(
        grid, {"history_query": HISTORY_QUERY, "history_period": "1 month"}
    )
    assert resolved.history_period == "1 month"
    assert resolved.complete_calendar_baselines == (f"{TS}.SeasonalNaive",)


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


def test_validator_reports_history_keys_with_their_path():
    from triage.adapters.run import validate_experiment_config

    report = validate_experiment_config(
        {"history_query": HISTORY_QUERY, "fill_missing_periods": "mean"}
    )
    paths = {e["path"] for e in report["errors"]}
    assert {"history_period", "fill_missing_periods"} <= paths
