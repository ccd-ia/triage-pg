"""Target-history point-in-time path (ADR-0030) — per-entity prior target history in two shapes.

The v1.0.1 time-series baselines need each entity's OWN prior target values, which the
cross-sectional design matrix (features + one forward label) does not carry. This module
produces that history under the ADR-0030 leakage boundary, in two shapes:

* **Windowed-label lags** (:func:`build_target_history_lags`) — reserved ``_target_lag_*``
  columns derived by reusing the label projection at prior as_of_dates. A label realized at
  date ``t`` over horizon ``w`` is admissible only where ``t + w <= as_of_date`` (its window
  must have fully elapsed to be knowable). Consumed by the cheap baselines
  (persistence/MA/drift/promedio).
* **Raw periodic series** (:func:`build_target_history_series`) — a per-entity series built
  from an *optional* ``history_query`` (a period-level aggregation whose rows are restricted to
  ``knowledge_date < as_of_date``), placed on the calendar by
  :func:`pivot_series_to_history_columns`: slot ``_hist_{width-k}`` holds the period that
  starts ``k × history_period`` before the as_of_date, and a missing period stays NULL.
  Consumed by seasonal-naive / Holt–Winters / Croston.

Both shapes are placed by calendar offset, never packed by rank: a missing period or a
missing label date is a visible gap, not a reason for an older value to slide into its slot.

Both are excluded from the feature set and from fit-based imputation (ADR-0009): the reserved
``_target_lag_`` prefix is filtered out of the matrix feature columns
(:func:`triage.adapters.matrix._feature_columns`), and the raw series is handed to the estimator
alongside X rather than joined as features.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
    "complete_calendar_baselines",
    "check_history_gaps",
    "place_series_on_calendar",
    "HistoryQueryRequired",
    "HistorySeriesError",
    "HistoryPeriodMisaligned",
    "HistoryGapsError",
    "HistoryPlacement",
    "PivotResult",
    "TargetHistory",
    "NO_TARGET_HISTORY",
    "FILL_MISSING_PERIODS",
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


# The values ``fill_missing_periods`` accepts. Unset (None) is the strict mode: a series with
# an interior gap fails the matrix build when a complete-calendar baseline is in the grid.
FILL_MISSING_PERIODS = ("zero", "null")

_TIMESERIES_MODULE = "triage.component.catwalk.baselines.timeseries"


class HistoryQueryRequired(ValueError):
    """A raw-series baseline is in the grid but no ``history_query`` was configured."""


class HistorySeriesError(ValueError):
    """The ``history_query`` returned rows that cannot be placed on the calendar."""


class HistoryPeriodMisaligned(HistorySeriesError):
    """A series row's ``period`` is not on the ``history_period`` grid of its as_of_date, or
    lies at/after the as_of_date (a leak)."""


class HistoryGapsError(HistorySeriesError):
    """A series has interior gaps, ``fill_missing_periods`` is unset, and a baseline that needs
    a complete calendar is in the grid."""


@dataclass(frozen=True)
class TargetHistory:
    """The resolved target-history knobs a matrix build reads (ADR-0030).

    ``n_lags`` > 0 attaches the windowed-label lags; ``history_query`` attaches the raw
    series, ``series_width`` slots wide, placed every ``history_period``.
    ``fill_missing_periods`` is ``None`` (strict), ``"zero"`` or ``"null"``.
    ``complete_calendar_baselines`` names the grid's estimators that need a gap-free series;
    it decides whether gaps fail the build, and is not part of matrix identity (the Parquet
    does not depend on it).
    """

    n_lags: int = 0
    history_query: str | None = None
    series_width: int = 0
    history_period: str | None = None
    fill_missing_periods: str | None = None
    complete_calendar_baselines: tuple[str, ...] = field(default=())

    @property
    def enabled(self) -> bool:
        return self.n_lags > 0 or bool(self.history_query)

    def identity(self) -> dict[str, Any]:
        """The matrix-identity entries.

        The three original keys are always present (a matrix without history hashes as it
        always has). The layout keys are added only when history is attached: they change
        the hash of every matrix built with history, so a matrix packed by rank (≤ 1.5.0) is
        never reused — through the logical_id fallback either, since this config enters both.
        """
        entries: dict[str, Any] = {
            "target_history_lags": self.n_lags,
            "history_query": self.history_query,
            "history_series_width": self.series_width,
        }
        if self.enabled:
            entries.update(
                {
                    "history_layout": "calendar",
                    "history_period": self.history_period,
                    "fill_missing_periods": self.fill_missing_periods,
                }
            )
        return entries


NO_TARGET_HISTORY = TargetHistory()


# ----------------------------------------------------------------- windowed-label lags (a)


def build_target_history_lags(
    db_engine: DictRowPool,
    label_hash: str,
    as_of_dates: Sequence[date],
    label_timespan: str,
    n_lags: int,
):
    """Point-in-time-correct windowed-label lags per ``(entity_id, as_of_date)``.

    Lag ``k`` is the entity's label at the ``k``-th most recent *admissible label date of the
    Experiment* — the same grid for every entity — not the entity's own ``k``-th label. The
    grid is the distinct label dates of this ``label_hash`` + ``label_timespan`` whose window
    has fully elapsed: ``g + label_timespan <= as_of_date``. An entity with no label at one
    grid date (outside the cohort that date, say) gets NULL in that lag; the older label stays
    in its own lag. Spacing follows the label grid, so a 6-month training frequency gives
    6-month lags. An entity/date with no admissible label at any grid date produces no row
    (cold start — the caller left-joins → NULL).

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
        f'max(lab.outcome) filter (where grid.lag_ix = {k}) as "{TARGET_LAG_PREFIX}{k}"'
        for k in range(1, n_lags + 1)
    )
    sql = cast(
        LiteralString,
        f"""
        with label_dates as (
            -- the Experiment's label-date grid for this labels artifact and horizon
            select distinct lab.as_of_date, lab.label_timespan
            from triage.labels lab
            where lab.label_hash     = %(label_hash)s
              and lab.label_timespan = %(label_timespan)s::interval
        ),
        grid as (
            select
                cur.as_of_date            as cur_date,
                lab.as_of_date            as lag_date,
                row_number() over (
                    partition by cur.as_of_date
                    order by lab.as_of_date desc
                )                         as lag_ix
            from (select distinct unnest(%(as_of_dates)s::date[]) as as_of_date) cur
            join label_dates lab
              on lab.as_of_date + lab.label_timespan <= cur.as_of_date  -- ADR-0030 boundary
        )
        select
            lab.entity_id            as entity_id,
            grid.cur_date            as as_of_date,
            {lag_cols}
        from grid
        join triage.labels lab
          on lab.label_hash     = %(label_hash)s
         and lab.label_timespan = %(label_timespan)s::interval
         and lab.as_of_date     = grid.lag_date
         and lab.outcome is not null
        where grid.lag_ix <= %(n_lags)s
        group by lab.entity_id, grid.cur_date
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


def complete_calendar_baselines(grid_config: Mapping[str, Any]) -> tuple[str, ...]:
    """The grid's time-series baselines that need a complete calendar (no interior gaps).

    Read from each class's ``requires_complete_calendar`` attribute, so the list lives with
    the estimators. Only classes in the time-series baseline module are imported.
    """
    found: list[str] = []
    for class_path in grid_config:
        module_name, _, cls_name = str(class_path).rpartition(".")
        if module_name != _TIMESERIES_MODULE:
            continue
        cls = getattr(importlib.import_module(module_name), cls_name)
        if getattr(cls, "requires_complete_calendar", False):
            found.append(str(class_path))
    return tuple(sorted(found))


def check_history_gaps(
    target_history: TargetHistory, gaps: Mapping[str, Any] | None
) -> None:
    """Raise :class:`HistoryGapsError` when the strict mode applies and the series has gaps.

    ``gaps`` is :meth:`PivotResult.summary` (also stored in the matrix metadata, so a reused
    matrix is checked against the grid of the run that reuses it).
    """
    if not gaps or not gaps.get("n_series_with_gaps"):
        return
    if target_history.fill_missing_periods is not None:
        return
    if not target_history.complete_calendar_baselines:
        return
    example = gaps.get("example") or {}
    short = ", ".join(
        c.rsplit(".", 1)[-1] for c in target_history.complete_calendar_baselines
    )
    raise HistoryGapsError(
        f"{gaps['n_series_with_gaps']} of {gaps['n_series']} target-history series have"
        + " missing periods inside their history (for example entity"
        + f" {example.get('entity_id')} at as_of_date {example.get('as_of_date')}: no row for"
        + f" period {example.get('first_missing_period')}). {short} need a complete calendar"
        + " and would forecast from the wrong months. Fix one of two ways: return a row for"
        + " every period from the history_query, zeros included (a generate_series calendar"
        + " left-joined to the counts), or set fill_missing_periods: zero to treat a missing"
        + " period as zero. Run `triage check history EXPERIMENT.yaml` to list the gaps."
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


@dataclass
class HistoryPlacement:
    """The long series placed on the calendar grid, before any fill or pivot.

    ``placed`` has ``entity_id, as_of_date, period, value, k`` (``k`` = periods back from the
    as_of_date, 1 = newest). ``pairs`` has one row per ``(entity_id, as_of_date)`` with
    ``k_max`` (offset of the oldest returned period), ``n_present`` and ``n_gaps`` (interior
    slots with no value). ``misaligned`` and ``leaked`` are the rows that fit no grid slot,
    each with ``nearest`` (the nearest grid date).
    """

    placed: Any
    pairs: Any
    misaligned: Any
    leaked: Any
    width: int
    period: str


def _period_step(period: str):
    from triage.util.conf import convert_str_to_relativedelta

    try:
        return convert_str_to_relativedelta(period)
    except ValueError as exc:
        raise ValueError(
            f"history_period {period!r} is not an interval such as '1 month' or '7 days'"
        ) from exc


def place_series_on_calendar(series_frame, width: int, period: str) -> HistoryPlacement:
    """Place the long ``(entity_id, as_of_date, period, value)`` series on the calendar grid.

    For each as_of_date ``a`` the grid is ``a − k·history_period`` for ``k = 1…width``. A row
    whose period is a grid date is placed at offset ``k``. A row inside
    ``[a − width·period, a)`` that is not a grid date is *misaligned*; a row at or after ``a``
    is *leaked*; rows older than the window are dropped. A row with a NULL value is not a
    value, so its slot counts as missing.
    """
    import polars as pl

    if width < 1:
        raise ValueError(f"history_series_width must be >= 1, got {width}")
    step = _period_step(period)
    frame = series_frame.filter(pl.col("value").is_not_null())
    as_of_dates = sorted(set(series_frame.get_column("as_of_date").to_list()))
    grid_rows = [
        {"as_of_date": a, "period": a - step * k, "k": k}
        for a in as_of_dates
        for k in range(1, width + 1)
    ]
    grid = pl.DataFrame(
        grid_rows,
        schema={"as_of_date": pl.Date, "period": pl.Date, "k": pl.Int64},
    )
    window = pl.DataFrame(
        [{"as_of_date": a, "window_start": a - step * width} for a in as_of_dates],
        schema={"as_of_date": pl.Date, "window_start": pl.Date},
    )

    placed = frame.join(grid, on=["as_of_date", "period"], how="inner")
    off_grid = (
        frame.join(grid, on=["as_of_date", "period"], how="anti")
        .join(window, on="as_of_date", how="left")
        .filter(pl.col("period") >= pl.col("window_start"))
        .drop("window_start")
    )
    leaked = off_grid.filter(pl.col("period") >= pl.col("as_of_date"))
    misaligned = off_grid.filter(pl.col("period") < pl.col("as_of_date"))
    if misaligned.height:
        nearest = grid.rename({"period": "nearest"}).sort("nearest")
        misaligned = misaligned.sort("period").join_asof(
            nearest.drop("k"),
            left_on="period",
            right_on="nearest",
            by="as_of_date",
            strategy="nearest",
            check_sortedness=False,  # both sides sorted on the join key just above
        )
    else:
        misaligned = misaligned.with_columns(
            pl.lit(None, dtype=pl.Date).alias("nearest")
        )

    dupes = (
        placed.group_by(["entity_id", "as_of_date", "k"])
        .len()
        .filter(pl.col("len") > 1)
    )
    if dupes.height:
        d = dupes.row(0, named=True)
        raise HistorySeriesError(
            f"history_query returned {dupes.height} duplicate (entity_id, period) row(s) — for"
            + f" example entity {d['entity_id']} at as_of_date {d['as_of_date']}, period"
            + f" {d['as_of_date'] - step * d['k']}. Return one row per entity and period"
            + " (aggregate with group by)."
        )

    pairs = (
        placed.group_by(["entity_id", "as_of_date"])
        .agg(
            pl.col("k").max().alias("k_max"),
            pl.len().cast(pl.Int64).alias("n_present"),
        )
        .with_columns((pl.col("k_max") - pl.col("n_present")).alias("n_gaps"))
        .sort(["entity_id", "as_of_date"])
    )
    return HistoryPlacement(
        placed=placed,
        pairs=pairs,
        misaligned=misaligned,
        leaked=leaked,
        width=width,
        period=period,
    )


@dataclass
class PivotResult:
    """:func:`pivot_series_to_history_columns`'s output: the wide frame plus its gap counts."""

    frame: Any
    n_series: int
    n_series_with_gaps: int
    example: dict[str, Any] | None

    def summary(self) -> dict[str, Any]:
        """JSON-able gap counts (stored in the matrix metadata)."""
        return {
            "n_series": self.n_series,
            "n_series_with_gaps": self.n_series_with_gaps,
            "example": self.example,
        }


def _first_gap_example(placement: HistoryPlacement, step) -> dict[str, Any] | None:
    import polars as pl

    gapped = placement.pairs.filter(pl.col("n_gaps") > 0)
    if not gapped.height:
        return None
    row = gapped.row(0, named=True)
    have = set(
        placement.placed.filter(
            (pl.col("entity_id") == row["entity_id"])
            & (pl.col("as_of_date") == row["as_of_date"])
        )
        .get_column("k")
        .to_list()
    )
    missing_k = max(k for k in range(1, row["k_max"] + 1) if k not in have)
    return {
        "entity_id": row["entity_id"],
        "as_of_date": row["as_of_date"].isoformat(),
        "first_missing_period": (row["as_of_date"] - step * missing_k).isoformat(),
    }


def _raise_off_grid(placement: HistoryPlacement) -> None:
    if placement.leaked.height:
        r = placement.leaked.row(0, named=True)
        raise HistoryPeriodMisaligned(
            f"history_query returned {placement.leaked.height} row(s) at or after their"
            + f" as_of_date — for example entity {r['entity_id']}, as_of_date"
            + f" {r['as_of_date']}, period {r['period']}. A period must start before the"
            + " as_of_date (point-in-time boundary, ADR-0030)."
        )
    if placement.misaligned.height:
        r = placement.misaligned.row(0, named=True)
        raise HistoryPeriodMisaligned(
            f"history_query returned {placement.misaligned.height} row(s) whose period is not"
            + f" on the history_period grid ({placement.period!r} steps back from each"
            + f" as_of_date) — for example entity {r['entity_id']}, as_of_date"
            + f" {r['as_of_date']}, period {r['period']}; the nearest grid date is"
            + f" {r['nearest']}. Truncate the period to the grid (date_trunc) or change"
            + " history_period."
        )


def pivot_series_to_history_columns(
    series_frame, width: int, period: str, fill: str | None = None
) -> PivotResult:
    """Place the long raw series on the calendar and pivot it to ``_hist_0 .. _hist_{width-1}``.

    Slot ``_hist_{width-k}`` holds the period that starts ``k × period`` before the
    as_of_date, so ``_hist_{width-1}`` is the newest period and every value sits at its own
    calendar offset. A period with no row is NULL. Slots older than the entity's oldest
    returned period are *leading* (the series starts later) and stay NULL in every mode; the
    slots between the oldest returned period and offset 1 with no row are *interior gaps*.
    ``fill="zero"`` writes ``0.0`` into interior gaps only; ``fill="null"`` and ``None`` leave
    them NULL (``None`` is the strict mode — :func:`check_history_gaps` decides whether it
    fails). Misaligned or leaked periods raise :class:`HistoryPeriodMisaligned`.
    """
    import polars as pl

    if fill not in (None, *FILL_MISSING_PERIODS):
        raise ValueError(
            f"fill_missing_periods must be one of {FILL_MISSING_PERIODS} or unset, got {fill!r}"
        )
    columns = [f"{HISTORY_SERIES_PREFIX}{s}" for s in range(width)]
    schema: dict[str, Any] = {"entity_id": pl.Int64, "as_of_date": pl.Date}
    schema.update({c: pl.Float64 for c in columns})
    if series_frame.height == 0:
        return PivotResult(pl.DataFrame(schema=schema), 0, 0, None)

    step = _period_step(period)
    placement = place_series_on_calendar(series_frame, width, period)
    _raise_off_grid(placement)
    placed = placement.placed.select(["entity_id", "as_of_date", "k", "value"])

    if fill == "zero":
        gapped = placement.pairs.filter(pl.col("n_gaps") > 0)
        if gapped.height:
            slots = (
                gapped.select(
                    "entity_id",
                    "as_of_date",
                    pl.int_ranges(1, pl.col("k_max") + 1).alias("k"),
                )
                .explode("k")
                .with_columns(pl.col("k").cast(pl.Int64))
            )
            zeros = slots.join(
                placed, on=["entity_id", "as_of_date", "k"], how="anti"
            ).with_columns(pl.lit(0.0).alias("value"))
            placed = pl.concat([placed, zeros.select(placed.columns)])

    wide = placed.with_columns(
        (width - pl.col("k")).cast(pl.Int64).alias("slot")
    ).pivot(
        values="value",
        index=["entity_id", "as_of_date"],
        on="slot",
        aggregate_function="first",
    )
    wide = wide.rename(
        {
            str(s): f"{HISTORY_SERIES_PREFIX}{s}"
            for s in range(width)
            if str(s) in wide.columns
        }
    )
    missing_cols = [
        pl.lit(None, dtype=pl.Float64).alias(c)
        for c in columns
        if c not in wide.columns
    ]
    if missing_cols:
        wide = wide.with_columns(missing_cols)
    wide = wide.select(
        pl.col("entity_id").cast(pl.Int64),
        pl.col("as_of_date").cast(pl.Date),
        *[pl.col(c).cast(pl.Float64) for c in columns],
    ).sort(["entity_id", "as_of_date"])

    n_with_gaps = placement.pairs.filter(pl.col("n_gaps") > 0).height
    return PivotResult(
        frame=wide,
        n_series=placement.pairs.height,
        n_series_with_gaps=n_with_gaps,
        example=_first_gap_example(placement, step),
    )


def resolve_target_history(
    grid_config: Mapping[str, Any], experiment_config: Mapping[str, Any]
) -> TargetHistory:
    """Resolve the :class:`TargetHistory` for the matrix build from the experiment config.

    Enforces the ADR-0030 contract (:func:`require_history_query`) and validates the query when
    present. ``n_lags > 0`` iff a lag-family baseline is in the grid (default
    :data:`DEFAULT_TARGET_HISTORY_LAGS`, overridable via ``target_history_lags``). A
    ``history_query`` needs ``history_period``; ``fill_missing_periods`` is one of
    :data:`FILL_MISSING_PERIODS` and is only valid with a ``history_query``. Raises
    ``ValueError`` naming the key on any violation.
    """
    history_query = experiment_config.get("history_query")
    require_history_query(grid_config, history_query)
    for _path, message in target_history_config_errors(experiment_config):
        raise ValueError(message)
    if history_query:
        validate_history_query(history_query)
    has_lag = any(c in LAG_BASELINE_CLASSES for c in grid_config)
    n_lags = (
        int(experiment_config.get("target_history_lags", DEFAULT_TARGET_HISTORY_LAGS))
        if has_lag
        else 0
    )
    series_width = (
        int(experiment_config.get("history_series_width", DEFAULT_HISTORY_SERIES_WIDTH))
        if history_query
        else 0
    )
    return TargetHistory(
        n_lags=n_lags,
        history_query=history_query or None,
        series_width=series_width,
        history_period=experiment_config.get("history_period")
        if history_query
        else None,
        fill_missing_periods=experiment_config.get("fill_missing_periods"),
        complete_calendar_baselines=complete_calendar_baselines(grid_config),
    )


def target_history_config_errors(
    experiment_config: Mapping[str, Any],
) -> list[tuple[str, str]]:
    """The ``history_period`` / ``fill_missing_periods`` errors in a config, as
    ``(path, message)`` pairs.

    Used by :func:`resolve_target_history` (raises the first) and by the dry-run validator
    (reports each with its key path).
    """
    errors: list[tuple[str, str]] = []
    history_query = experiment_config.get("history_query")
    period = experiment_config.get("history_period")
    fill = experiment_config.get("fill_missing_periods")
    if history_query and not period:
        errors.append(
            (
                "history_period",
                "history_period is required with a history_query: the length of one period of the"
                + " series, so each value is placed at its calendar offset — for example"
                + " history_period: '1 month'",
            )
        )
    if period and not history_query:
        errors.append(
            (
                "history_period",
                "history_period is set but there is no history_query to place",
            )
        )
    if period:
        try:
            _period_step(str(period))
        except ValueError as exc:
            errors.append(("history_period", str(exc)))
    if fill is not None and fill not in FILL_MISSING_PERIODS:
        errors.append(
            (
                "fill_missing_periods",
                "fill_missing_periods must be 'zero' or 'null' (or unset for strict),"
                + f" got {fill!r}",
            )
        )
    if fill is not None and not history_query:
        errors.append(
            (
                "fill_missing_periods",
                "fill_missing_periods is set but there is no history_query to fill",
            )
        )
    return errors
