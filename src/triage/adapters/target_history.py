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
    "RAW_SERIES_BASELINE_CLASSES",
    "build_target_history_lags",
    "build_target_history_series",
    "validate_history_query",
    "require_history_query",
    "HistoryQueryRequired",
]

# Reserved column namespace for the windowed-label lags (ADR-0030 / decision D2). Matrix
# assembly excludes anything with this prefix from the feature set + imputation.
TARGET_LAG_PREFIX = "_target_lag_"

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
