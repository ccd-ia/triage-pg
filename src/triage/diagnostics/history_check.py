"""``triage check history``: is the target-history series complete on the calendar?

The raw-series baselines read the ``history_query`` placed on a calendar grid: one slot per
``history_period`` before each as_of_date. A period the query does not return is a gap, and a
period that is not on the grid cannot be placed at all. This check renders the query for a
few as_of_dates, runs the same placement the matrix build runs
(:func:`triage.adapters.target_history.place_series_on_calendar`) and reports what it finds,
so a config can be fixed before a run. It builds nothing and writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from triage.adapters.target_history import (
    DEFAULT_HISTORY_SERIES_WIDTH,
    build_target_history_series,
    place_series_on_calendar,
    target_history_config_errors,
    validate_history_query,
)
from triage.util.db import DictRowPool

__all__ = ["HistoryCheckError", "HistoryCheckReport", "check_history", "default_dates"]


class HistoryCheckError(ValueError):
    """The check cannot run: no history_query, a bad key, or a bad date."""


@dataclass
class HistoryCheckReport:
    """What :func:`check_history` found, per the whole set of checked dates."""

    as_of_dates: list[date]
    history_period: str
    width: int
    fill_missing_periods: str | None
    n_series: int = 0
    n_series_with_gaps: int = 0
    n_gaps: int = 0
    n_misaligned: int = 0
    n_leaked: int = 0
    worst_entities: list[dict[str, Any]] = field(default_factory=list)
    misaligned_examples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.n_series_with_gaps or self.n_misaligned or self.n_leaked)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_dates": [d.isoformat() for d in self.as_of_dates],
            "history_period": self.history_period,
            "history_series_width": self.width,
            "fill_missing_periods": self.fill_missing_periods,
            "n_series": self.n_series,
            "n_series_with_gaps": self.n_series_with_gaps,
            "n_gaps": self.n_gaps,
            "n_misaligned": self.n_misaligned,
            "n_leaked": self.n_leaked,
            "worst_entities": self.worst_entities,
            "misaligned_examples": self.misaligned_examples,
            "clean": self.clean,
        }


def default_dates(config: Mapping[str, Any]) -> list[date]:
    """The last test as_of_date of the config's temporal_config."""
    from pydantic import ValidationError

    from triage.adapters.run import _as_dates, _generate_splits
    from triage.adapters.temporal import TemporalConfig

    raw = config.get("temporal_config")
    if not isinstance(raw, Mapping):
        raise HistoryCheckError("the experiment config needs a temporal_config mapping")
    try:
        splits = _generate_splits(TemporalConfig.model_validate(dict(raw)))
    except (ValidationError, ValueError) as exc:
        raise HistoryCheckError(f"temporal_config is not valid: {exc}") from exc
    test_dates = [
        d
        for split in splits
        for test in split["test_matrices"]
        for d in _as_dates(list(test["as_of_times"]))
    ]
    if not test_dates:
        raise HistoryCheckError("the temporal_config produces no test as_of_date")
    return [max(test_dates)]


def check_history(
    db_engine: DictRowPool,
    config: Mapping[str, Any],
    as_of_dates: Sequence[date] | None = None,
    top: int = 5,
) -> HistoryCheckReport:
    """Render the ``history_query`` at ``as_of_dates`` and report gaps and off-grid periods.

    Args:
        db_engine: the project database.
        config: the experiment config; reads ``history_query``, ``history_period``,
            ``history_series_width``, ``fill_missing_periods`` and, for the default date,
            ``temporal_config``.
        as_of_dates: the dates to render the query at. Default: :func:`default_dates`.
        top: how many entities to list, most gaps first.

    Raises:
        HistoryCheckError: on a missing ``history_query``, an invalid key, or a query that
            does not render.
    """
    import polars as pl

    query = config.get("history_query")
    if not query:
        raise HistoryCheckError(
            "the config has no history_query — there is no target-history series to check"
        )
    errors = target_history_config_errors(config)
    if errors:
        raise HistoryCheckError("; ".join(f"{path}: {msg}" for path, msg in errors))
    try:
        validate_history_query(str(query))
    except ValueError as exc:
        raise HistoryCheckError(str(exc)) from exc
    period = str(config["history_period"])
    width = int(config.get("history_series_width", DEFAULT_HISTORY_SERIES_WIDTH))
    dates = list(as_of_dates) if as_of_dates else default_dates(config)

    report = HistoryCheckReport(
        as_of_dates=sorted(dates),
        history_period=period,
        width=width,
        fill_missing_periods=config.get("fill_missing_periods"),
    )
    series = build_target_history_series(db_engine, str(query), report.as_of_dates)
    if series.height == 0:
        return report
    placement = place_series_on_calendar(series, width, period)
    pairs = placement.pairs
    report.n_series = pairs.height
    report.n_series_with_gaps = pairs.filter(pl.col("n_gaps") > 0).height
    report.n_gaps = int(pairs.get_column("n_gaps").sum() or 0)
    report.n_misaligned = placement.misaligned.height
    report.n_leaked = placement.leaked.height
    worst = (
        pairs.filter(pl.col("n_gaps") > 0)
        .group_by("entity_id")
        .agg(
            pl.col("n_gaps").sum().alias("n_gaps"),
            pl.col("n_present").sum().alias("n_present"),
        )
        .sort(["n_gaps", "entity_id"], descending=[True, False])
        .head(top)
    )
    report.worst_entities = worst.to_dicts()
    off_grid = pl.concat(
        [
            placement.misaligned.select(
                "entity_id",
                "as_of_date",
                "period",
                pl.col("nearest").alias("nearest_grid_date"),
                pl.lit("off the grid").alias("problem"),
            ),
            placement.leaked.select(
                "entity_id",
                "as_of_date",
                "period",
                pl.lit(None, dtype=pl.Date).alias("nearest_grid_date"),
                pl.lit("at or after the as_of_date").alias("problem"),
            ),
        ]
    ).head(top)
    report.misaligned_examples = [
        {k: (v.isoformat() if isinstance(v, date) else v) for k, v in row.items()}
        for row in off_grid.to_dicts()
    ]
    return report
