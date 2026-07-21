"""Target-history point-in-time path (ADR-0030) — per-entity prior target history in two shapes.

The v1.0.1 time-series baselines need each entity's OWN prior target values, which the
cross-sectional design matrix (features + one forward label) does not carry. This module
produces that history under the ADR-0030 leakage boundary, in two shapes:

* **Windowed-label lags** (:func:`build_target_history_lags`) — reserved ``_target_lag_*``
  columns derived by reusing the label projection at prior as_of_dates. A label realized at
  date ``t`` over horizon ``w`` is admissible only where ``t + w <= as_of_date`` (its window
  must have fully elapsed to be knowable). Consumed by the cheap baselines
  (persistence/MA/drift/promedio).
* **Raw periodic series** (:func:`build_target_history_series`) — a variable-length per-entity
  series built from an *optional* ``history_query`` (a period-level aggregation whose rows are
  restricted to ``knowledge_date < as_of_date``). Consumed by seasonal-naive / Holt–Winters /
  Croston.

Both are excluded from the feature set and from fit-based imputation (ADR-0009): the reserved
``_target_lag_`` prefix is filtered out of the matrix feature columns
(:func:`triage.adapters.matrix._feature_columns`), and the raw series is handed to the estimator
alongside X rather than joined as features.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, LiteralString, cast

from triage.logging import get_logger
from triage.util.db import DictRowPool

logger = get_logger(__name__)

__all__ = [
    "TARGET_LAG_PREFIX",
    "HISTORY_SERIES_PREFIX",
    "DEFAULT_TARGET_HISTORY_LAGS",
    "DEFAULT_HISTORY_SERIES_WIDTH",
    "RAW_SERIES_BASELINE_CLASSES",
    "LAG_BASELINE_CLASSES",
    "build_target_history_lags",
    "build_target_history_series",
    "pivot_series_to_history_columns",
    "history_columns_in_order",
    "is_reserved_history_column",
    "resolve_target_history",
    "validate_history_query",
    "require_history_query",
    "HistoryQueryRequired",
]

# Reserved column namespaces (ADR-0030 / decision D2). Matrix assembly excludes anything with
# these prefixes from the feature set + imputation; the estimator seam feeds them to a
# consumes_target_history estimator by its history_kind ("lags" -> _target_lag_*, "series" ->
# _hist_*).
TARGET_LAG_PREFIX = "_target_lag_"
HISTORY_SERIES_PREFIX = "_hist_"

# Defaults when a target-history baseline is in the grid: how many windowed-label lags to
# attach, and the max width the raw periodic series is padded/truncated to (both overridable
# via experiment_config: target_history_lags / history_series_width).
DEFAULT_TARGET_HISTORY_LAGS = 12
DEFAULT_HISTORY_SERIES_WIDTH = 24

# The Phase-4 raw-series baseline class paths — the estimators that consume the periodic
# sidecar and therefore REQUIRE a ``history_query`` (ADR-0030). Kept here (not in the
# timeseries module) so the config validator can enforce the contract without importing the
# estimators; Phase 4 implements the classes at exactly these paths.
RAW_SERIES_BASELINE_CLASSES = frozenset(
    {
        "triage.component.catwalk.baselines.timeseries.SeasonalNaive",
        "triage.component.catwalk.baselines.timeseries.HoltWinters",
        "triage.component.catwalk.baselines.timeseries.ETS",
        "triage.component.catwalk.baselines.timeseries.Croston",
        "triage.component.catwalk.baselines.timeseries.CrostonSBA",
    }
)

# The lag-family baselines — they read the reserved _target_lag_* columns (windowed-label
# lags), so a matrix built for them must carry those lags (n_lags > 0). No history_query needed.
LAG_BASELINE_CLASSES = frozenset(
    {
        "triage.component.catwalk.baselines.timeseries.Persistence",
        "triage.component.catwalk.baselines.timeseries.PromedioDisponible",
        "triage.component.catwalk.baselines.timeseries.MovingAverage",
        "triage.component.catwalk.baselines.timeseries.Drift",
    }
)

_HISTORY_QUERY_PLACEHOLDER = "{as_of_date}"


class HistoryQueryRequired(ValueError):
    """A raw-series baseline is in the grid but no ``history_query`` was configured."""


# ----------------------------------------------------------------- windowed-label lags (a)


def build_target_history_lags(
    db_engine: DictRowPool,
    label_hash: str,
    as_of_dates: Sequence[date],
    label_timespan: str,
    n_lags: int,
):
    """Point-in-time-correct windowed-label lags per ``(entity_id, as_of_date)``.

    For each current ``as_of_date``, gather the entity's prior labels (same ``label_hash`` +
    ``label_timespan``) whose horizon has fully elapsed — ``prior.as_of_date + label_timespan
    <= as_of_date`` — ranked most-recent-first into ``_target_lag_1 .. _target_lag_{n_lags}``.
    An entity/date with fewer than ``n_lags`` admissible priors gets NULLs for the missing
    lags; one with none produces no row at all (cold-start — the caller left-joins → NULL).

    The ``t + w <= as_of_date`` predicate IS the leakage boundary (ADR-0030): a label whose
    window has not closed by ``as_of_date`` is not yet knowable and must never appear.

    Returns:
        A Polars DataFrame with columns ``entity_id, as_of_date, _target_lag_1 …`` (only the
        rows that have at least one admissible prior).
    """
    import polars as pl

    if n_lags < 1:
        raise ValueError(f"n_lags must be >= 1, got {n_lags}")

    # The lag columns are a fixed, code-controlled fan-out (n_lags is an int the caller sets,
    # never user SQL), so building the conditional-aggregation column list by hand is safe.
    lag_cols = ",\n            ".join(
        f'max(outcome) filter (where lag_ix = {k}) as "{TARGET_LAG_PREFIX}{k}"'
        for k in range(1, n_lags + 1)
    )
    sql = cast(
        LiteralString,
        f"""
        with ranked as (
            select
                cur.as_of_date            as as_of_date,
                lab.entity_id             as entity_id,
                lab.outcome               as outcome,
                row_number() over (
                    partition by lab.entity_id, cur.as_of_date
                    order by lab.as_of_date desc
                )                         as lag_ix
            from (select distinct unnest(%(as_of_dates)s::date[]) as as_of_date) cur
            join triage.labels lab
              on lab.label_hash     = %(label_hash)s
             and lab.label_timespan = %(label_timespan)s::interval
             and lab.as_of_date + lab.label_timespan <= cur.as_of_date  -- ADR-0030 boundary
             and lab.outcome is not null
        )
        select
            entity_id,
            as_of_date,
            {lag_cols}
        from ranked
        where lag_ix <= %(n_lags)s
        group by entity_id, as_of_date
        """,
    )
    with db_engine.connection() as conn:
        rows = conn.execute(
            sql,
            {
                "as_of_dates": list(as_of_dates),
                "label_hash": label_hash,
                "label_timespan": label_timespan,
                "n_lags": n_lags,
            },
        ).fetchall()

    lag_columns = [f"{TARGET_LAG_PREFIX}{k}" for k in range(1, n_lags + 1)]
    schema: dict[str, Any] = {"entity_id": pl.Int64, "as_of_date": pl.Date}
    for col in lag_columns:
        schema[col] = pl.Float64
    if not rows:
        return pl.DataFrame(schema=schema)
    frame = pl.DataFrame(rows, schema_overrides=schema)
    logger.debug(
        "target-history lags: %d (entity, as_of_date) rows × %d lag(s) from labels %s…",
        frame.height,
        n_lags,
        label_hash[:12],
    )
    return frame


# ---------------------------------------------------------------- raw periodic series (b)


def build_target_history_series(
    db_engine: DictRowPool,
    history_query_template: str,
    as_of_dates: Sequence[date],
):
    """Point-in-time-correct raw periodic sidecar from the optional ``history_query``.

    The template is a single SELECT returning ``entity_id, period, value`` and carrying an
    ``{as_of_date}`` placeholder; it is rendered once per as_of_date (``{as_of_date}`` → a
    quoted date literal, mirroring :mod:`triage.adapters.labels`) so each row is the entity's
    history AS OF that date. The query is responsible for its own knowledge-date discipline —
    ``where knowledge_date < {as_of_date}`` — which is the raw-series leakage boundary
    (ADR-0030); nothing knowable only at/after ``as_of_date`` may be selected.

    Returns:
        A long Polars DataFrame ``entity_id, as_of_date, period, value`` sorted by
        ``(entity_id, as_of_date, period)`` — the per-entity series the raw-series baselines
        group over.
    """
    import polars as pl

    validate_history_query(history_query_template)
    schema = {
        "entity_id": pl.Int64,
        "as_of_date": pl.Date,
        "period": pl.Date,
        "value": pl.Float64,
    }
    collected: list[dict[str, Any]] = []
    with db_engine.connection() as conn:
        for as_of_date in as_of_dates:
            rendered = history_query_template.format(as_of_date=f"'{as_of_date}'")
            # Literal '%' in the user's SQL must be doubled — psycopg3 reads '%' as a marker
            # (mirrors triage.adapters.labels). No bind params remain after rendering.
            sql = cast(
                LiteralString,
                f"select entity_id, period, value from ({rendered.replace('%', '%%')}) sub",
            )
            for r in conn.execute(sql).fetchall():
                collected.append(
                    {
                        "entity_id": r["entity_id"],
                        "as_of_date": as_of_date,
                        "period": r["period"],
                        "value": r["value"],
                    }
                )
    if not collected:
        return pl.DataFrame(schema=schema)
    frame = pl.DataFrame(collected, schema_overrides=schema).sort(
        ["entity_id", "as_of_date", "period"]
    )
    logger.debug(
        "target-history raw series: %d rows over %d as_of_date(s)",
        frame.height,
        len(as_of_dates),
    )
    return frame


# ----------------------------------------------------------------------- config validation


def validate_history_query(history_query_template: str) -> None:
    """Validate a ``history_query`` template (mirrors ``labels._validate_template``)."""
    if _HISTORY_QUERY_PLACEHOLDER not in history_query_template:
        raise ValueError(
            f"history_query must contain the {_HISTORY_QUERY_PLACEHOLDER} placeholder"
            + " (ADR-0030 raw-series contract); got: "
            + repr(history_query_template)
        )
    if ";" in history_query_template:
        raise ValueError(
            "history_query must be a single SELECT with no ';' — it is wrapped as a subquery;"
            + " got: "
            + repr(history_query_template)
        )


def require_history_query(
    grid_config: Mapping[str, Any], history_query: str | None
) -> None:
    """Enforce the ADR-0030 contract: a raw-series baseline in ``grid_config`` requires a
    ``history_query``. Raises :class:`HistoryQueryRequired` naming the offending estimator(s).
    """
    raw_in_grid = [c for c in grid_config if c in RAW_SERIES_BASELINE_CLASSES]
    if raw_in_grid and not history_query:
        raise HistoryQueryRequired(
            "grid_config uses raw-series baseline(s) "
            + f"{sorted(raw_in_grid)} which forecast over the raw periodic series, but no"
            + " 'history_query' is configured. Add a history_query (a period-level aggregation"
            + " with 'where knowledge_date < {as_of_date}') or drop those estimators (ADR-0030)."
        )


# ----------------------------------------------------------------- reserved-column plumbing


def is_reserved_history_column(name: str) -> bool:
    """True for a reserved target-history column (lag or raw-series), excluded from features."""
    return name.startswith(TARGET_LAG_PREFIX) or name.startswith(HISTORY_SERIES_PREFIX)


def history_columns_in_order(frame_columns, history_kind: str) -> list[str]:
    """The reserved history columns an estimator reads, ordered CHRONOLOGICALLY (oldest ->
    newest) so ``timeseries`` forecasters' ``_forecast_one`` sees a chronological series.

    * ``"lags"`` -> ``_target_lag_*`` reversed (``_target_lag_n`` is oldest, ``_target_lag_1``
      is the most recent).
    * ``"series"`` -> ``_hist_0 .. _hist_{m-1}`` in index order (``_hist_0`` is oldest).
    """
    if history_kind == "lags":
        cols = [c for c in frame_columns if c.startswith(TARGET_LAG_PREFIX)]
        cols.sort(key=lambda c: int(c[len(TARGET_LAG_PREFIX) :]))
        return list(reversed(cols))
    if history_kind == "series":
        cols = [c for c in frame_columns if c.startswith(HISTORY_SERIES_PREFIX)]
        cols.sort(key=lambda c: int(c[len(HISTORY_SERIES_PREFIX) :]))
        return cols
    raise ValueError(
        f"unknown history_kind {history_kind!r} (expected 'lags' | 'series')"
    )


def pivot_series_to_history_columns(series_frame, width: int):
    """Pivot the long raw-series sidecar into reserved ``_hist_0 .. _hist_{width-1}`` columns.

    ``series_frame`` is the long ``(entity_id, as_of_date, period, value)`` frame from
    :func:`build_target_history_series`. Within each ``(entity_id, as_of_date)`` the periods are
    ranked chronologically and the last ``width`` kept (most recent history), left-padded so
    ``_hist_{width-1}`` is always the newest; shorter histories leave the older slots NULL. The
    estimator seam reads these as one chronological series (NaN = missing).
    """
    import polars as pl

    if series_frame.height == 0:
        return pl.DataFrame(schema={"entity_id": pl.Int64, "as_of_date": pl.Date})
    # Rank periods newest-first within each (entity, as_of_date); keep the last `width`. The
    # newest kept period lands in slot width-1, the oldest kept in a lower slot -> left-padded,
    # so _hist_0.._hist_{width-1} reads oldest -> newest with missing older slots left NULL.
    ranked = series_frame.with_columns(
        pl.col("period")
        .rank("ordinal", descending=True)
        .over(["entity_id", "as_of_date"])
        .alias("rk")
    )
    ranked = ranked.filter(pl.col("rk") <= width).with_columns(
        (width - pl.col("rk")).cast(pl.Int64).alias("slot")
    )
    wide = ranked.pivot(
        values="value",
        index=["entity_id", "as_of_date"],
        on="slot",
        aggregate_function="first",
    )
    rename = {
        str(s): f"{HISTORY_SERIES_PREFIX}{s}"
        for s in range(width)
        if str(s) in wide.columns
    }
    return wide.rename(rename)


def resolve_target_history(
    grid_config: Mapping[str, Any], experiment_config: Mapping[str, Any]
) -> tuple[int, str | None]:
    """Resolve ``(n_lags, history_query)`` for the matrix build from the experiment config.

    Enforces the ADR-0030 contract (:func:`require_history_query`) and validates the query when
    present. ``n_lags > 0`` iff a lag-family baseline is in the grid (default
    :data:`DEFAULT_TARGET_HISTORY_LAGS`, overridable via ``experiment_config['target_history_lags']``);
    ``history_query`` is required iff a raw-series baseline is in the grid.
    """
    history_query = experiment_config.get("history_query")
    require_history_query(grid_config, history_query)
    if history_query:
        validate_history_query(history_query)
    has_lag = any(c in LAG_BASELINE_CLASSES for c in grid_config)
    n_lags = (
        int(experiment_config.get("target_history_lags", DEFAULT_TARGET_HISTORY_LAGS))
        if has_lag
        else 0
    )
    return n_lags, history_query
