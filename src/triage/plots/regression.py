"""One regression model's errors: forecast against outcome, residuals, bias, segment sums.

Every figure here reads a ``regression`` or ``regression_ranking`` model and refuses the
other types (:mod:`triage.problem_types`), naming the command that answers for them.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from matplotlib.colors import LinearSegmentedColormap

from triage import problem_types
from triage.diagnostics import readers
from triage.plots.style import (
    BASE_DASH,
    BLUE,
    INK,
    ORANGE,
    NothingToDraw,
    PlotResult,
    as_date,
    check_series,
    nan,
    new_figure,
    no_grid,
    span,
    style,
)
from triage.util.db import DictRowPool

#: Hexagons across the x axis of a hexbin.
HEX_GRIDSIZE = 40

# A one-hue ramp: few rows in a hexagon read pale, many read as full blue.
_RAMP = LinearSegmentedColormap.from_list("triage-blue", ["#dce9fa", BLUE])


def _log1p(value: float) -> float:
    """``sign(v)·log1p(|v|)``: log1p for counts, and defined for a negative forecast."""
    return math.copysign(math.log1p(abs(value)), value)


def _log1p_ticks(ax: Any, axis: str, values: list[float]) -> None:
    """Ticks at 0, 1, 10, 100… on an axis drawn in :func:`_log1p` units."""
    hi = max((abs(v) for v in values), default=1.0)
    raw = [0.0] + [10.0**k for k in range(0, int(math.log10(hi + 1)) + 2)]
    if min(values, default=0.0) < 0:
        raw = [-v for v in reversed(raw[1:])] + raw
    ticks = [_log1p(v) for v in raw]
    labels = [f"{v:g}" for v in raw]
    if axis == "x":
        ax.set_xticks(ticks, labels)
    else:
        ax.set_yticks(ticks, labels)


def _no_rows(model_id: int) -> NothingToDraw:
    return NothingToDraw(
        f"Model {model_id} has no labeled test rows — score and evaluate it first:"
        " triage run <config>."
    )


def _pairs(pool: DictRowPool, command: str, model_id: int) -> dict[str, Any]:
    problem_types.require(pool, command, model_id)
    result = readers.forecast_actual(pool, model_id)
    if not result["pairs"]:
        raise _no_rows(model_id)
    return result


def _sample_note(result: dict[str, Any]) -> str:
    kept = len(result["pairs"])
    if result["sampled"]:
        return f"{kept} of {result['n_total']} rows (sampled by entity)"
    return f"{kept} rows"


def forecast_actual(
    pool: DictRowPool, model_id: int, title: Optional[str] = None
) -> PlotResult:
    """Forecast against outcome as a hexbin on log1p axes, with the forecast = outcome line.

    Args:
        pool: the project database.
        model_id: a regression model.
        title: replaces the default title.

    Returns:
        ``data`` holds the :func:`readers.forecast_actual` result.

    Raises:
        WrongProblemType: the model is not a regression model.
        NothingToDraw: the model has no labeled test rows.
    """
    result = _pairs(pool, "plot forecast-actual", model_id)
    pairs = result["pairs"]
    xs = [_log1p(r["forecast"]) for r in pairs]
    ys = [_log1p(r["outcome"]) for r in pairs]
    with style():
        fig, axes = new_figure(5.6, 4.6)
        ax = axes[0, 0]
        hb = ax.hexbin(xs, ys, gridsize=HEX_GRIDSIZE, bins="log", mincnt=1, cmap=_RAMP)
        lo, hi = min(xs + ys), max(xs + ys)
        ax.plot([lo, hi], [lo, hi], color=INK, linestyle=BASE_DASH, linewidth=1.0)
        _log1p_ticks(ax, "x", [r["forecast"] for r in pairs])
        _log1p_ticks(ax, "y", [r["outcome"] for r in pairs])
        ax.set_xlabel("forecast (log1p scale)")
        ax.set_ylabel("outcome (log1p scale)")
        ax.set_title(title or f"Does model {model_id} forecast what happened?")
        fig.colorbar(hb, ax=ax, label="rows (log)")
        ax.grid(visible=False)
    over = sum(1 for r in pairs if r["forecast"] > r["outcome"])
    summary = (
        f"{_sample_note(result)}: forecast above the outcome on {over / len(pairs):.0%}"
    )
    return PlotResult(fig, {"forecast_actual": result}, summary)


def residuals(
    pool: DictRowPool, model_id: int, title: Optional[str] = None
) -> PlotResult:
    """Residual (forecast − outcome) against the forecast, as a hexbin.

    Args:
        pool: the project database.
        model_id: a regression model.
        title: replaces the default title.

    Returns:
        ``data`` holds the :func:`readers.forecast_actual` result.

    Raises:
        WrongProblemType: the model is not a regression model.
        NothingToDraw: the model has no labeled test rows.
    """
    result = _pairs(pool, "plot residuals", model_id)
    pairs = result["pairs"]
    xs = [_log1p(r["forecast"]) for r in pairs]
    errors = [r["forecast"] - r["outcome"] for r in pairs]
    with style():
        fig, axes = new_figure(5.6, 4.2)
        ax = axes[0, 0]
        hb = ax.hexbin(
            xs, errors, gridsize=HEX_GRIDSIZE, bins="log", mincnt=1, cmap=_RAMP
        )
        ax.axhline(0, color=INK, linestyle=BASE_DASH, linewidth=1.0)
        _log1p_ticks(ax, "x", [r["forecast"] for r in pairs])
        ax.set_xlabel("forecast (log1p scale)")
        ax.set_ylabel("forecast − outcome")
        ax.set_title(title or f"Where are model {model_id}'s errors?")
        fig.colorbar(hb, ax=ax, label="rows (log)")
        ax.grid(visible=False)
    mean = sum(errors) / len(errors)
    summary = (
        f"{_sample_note(result)}: mean residual {mean:.4g},"
        f" range {min(errors):.4g}–{max(errors):.4g}"
    )
    return PlotResult(fig, {"forecast_actual": result}, summary)


def bias(
    pool: DictRowPool,
    model_id: int,
    by: str = "date",
    title: Optional[str] = None,
) -> PlotResult:
    """Mean forecast − outcome per test date, or per segment pooled over the dates.

    Args:
        pool: the project database.
        model_id: a regression model.
        by: ``date``, or a segment cut of :func:`readers.residuals_by_segment`.
        title: replaces the default title.

    Returns:
        ``data`` holds ``'rows'``: the :func:`readers.residual_summary` rows (by date) or
        the pooled :func:`readers.residuals_by_segment` rows.

    Raises:
        WrongProblemType: the model is not a regression model.
        ValueError: ``by`` is not ``date`` or a cut.
        NothingToDraw: the model has no labeled test rows.
    """
    problem_types.require(pool, "plot bias", model_id)
    if by == "date":
        rows = readers.residual_summary(pool, model_id)
        labels = [str(as_date(r["as_of_date"])) for r in rows]
    else:
        rows = readers.residuals_by_segment(pool, model_id, by, pooled=True)
        labels = [r["segment"] for r in rows]
    if not rows:
        raise _no_rows(model_id)
    values = [nan(r["bias"]) for r in rows]
    with style():
        fig, axes = new_figure(max(4.8, 0.55 * len(rows) + 2.0), 3.6)
        ax = axes[0, 0]
        ax.axhline(0, color=INK, linewidth=0.8)
        if by == "date":
            ax.plot(labels, values, color=BLUE, marker="o", markersize=4)
            ax.tick_params(axis="x", labelrotation=45)
        else:
            colors = [BLUE if v >= 0 else ORANGE for v in values]
            ax.bar(labels, values, color=colors)
            no_grid(ax, "x")
        ax.set_ylabel("mean forecast − outcome")
        ax.set_xlabel("test date" if by == "date" else by)
        ax.set_title(title or f"Does model {model_id} over- or under-forecast?")
    summary = (
        f"bias by {by} over {len(rows)} {'date' if by == 'date' else 'segment'}(s):"
        f" {span([r['bias'] for r in rows], 3)}"
    )
    return PlotResult(fig, {"rows": rows}, summary)


def forecast_time(
    pool: DictRowPool,
    model_id: int,
    cut: str = "band",
    title: Optional[str] = None,
) -> PlotResult:
    """Σ forecast and Σ outcome per test date, one panel per segment of ``cut``.

    Args:
        pool: the project database.
        model_id: a regression model.
        cut: a segment cut of :func:`readers.residuals_by_segment`.
        title: replaces the default title.

    Returns:
        ``data`` maps each segment to its per-date rows.

    Raises:
        WrongProblemType: the model is not a regression model.
        ValueError: ``cut`` is not a cut, or it has more segments than one figure draws.
        NothingToDraw: the model has no labeled test rows.
    """
    problem_types.require(pool, "plot forecast-time", model_id)
    rows = readers.residuals_by_segment(pool, model_id, cut, pooled=False)
    if not rows:
        raise _no_rows(model_id)
    segments: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda r: (r["segment_order"], r["segment"])):
        segments.setdefault(r["segment"], []).append(r)
    check_series(len(segments), "segments", "pass a cut with fewer segments")
    ncols = min(len(segments), 3)
    nrows = math.ceil(len(segments) / ncols)
    with style():
        fig, axes = new_figure(3.4 * ncols, 2.6 * nrows + 0.4, nrows, ncols)
        flat = [ax for row in axes for ax in row]
        for ax, (segment, seg_rows) in zip(flat, segments.items(), strict=False):
            seg_rows.sort(key=lambda r: as_date(r["as_of_date"]))
            dates = [as_date(r["as_of_date"]) for r in seg_rows]
            ax.plot(dates, [r["sum_outcome"] for r in seg_rows], color=INK,
                    marker="o", markersize=3, label="Σ outcome")  # fmt: skip
            ax.plot(dates, [r["sum_forecast"] for r in seg_rows], color=BLUE,
                    marker="o", markersize=3, label="Σ forecast")  # fmt: skip
            ax.set_title(segment, fontsize=9.5)
            ax.tick_params(axis="x", labelrotation=45, labelsize=7.5)
        for ax in flat[len(segments) :]:
            ax.set_visible(False)
        flat[0].legend(loc="upper left")
        fig.suptitle(
            title or f"Model {model_id}: total forecast against total outcome by {cut}",
            color=INK,
            fontsize=10.5,
            fontweight="bold",
            x=0.01,
            ha="left",
        )
    ratios: list[Optional[float]] = []
    for seg_rows in segments.values():
        outcome = sum(r["sum_outcome"] for r in seg_rows)
        ratios.append(
            sum(r["sum_forecast"] for r in seg_rows) / outcome if outcome else None
        )
    n_dates = len({r["as_of_date"] for r in rows})
    summary = (
        f"{len(segments)} segment(s) by {cut} over {n_dates} date(s);"
        f" Σ forecast / Σ outcome {span(ratios, 2)}"
    )
    return PlotResult(fig, segments, summary)
