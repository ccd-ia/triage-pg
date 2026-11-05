"""The crosstabs: what characterizes a model's list, for one or two models, and across a
model group's test dates."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Optional

from matplotlib.lines import Line2D

from triage.diagnostics import readers
from triage.plots.style import (
    BLUE,
    INK,
    ORANGE,
    SERIES,
    NothingToDraw,
    PlotResult,
    as_date,
    check_series,
    log_ratio_axis,
    nan,
    new_figure,
    no_grid,
    only_cut,
    pct_axis,
    style,
)
from triage.util.db import DictRowPool

_ALL = 1_000_000
_STAT_NAME = {"mean": "mean ratio", "nonzero_rate": "nonzero rate"}


def _crosstabs_command(model: int | str, parameter: Optional[str]) -> str:
    return f"triage postmodel crosstabs {model} -p {parameter or '<cut>'}"


def _check_stat(stat: str) -> None:
    if stat not in readers.CROSSTAB_STATS:
        raise ValueError(
            f"stat={stat!r} is not a crosstab ranking — expected one of"
            f" {', '.join(readers.CROSSTAB_STATS)}"
        )


def _resolve(
    pool: DictRowPool,
    model_id: int,
    parameter: Optional[str],
    as_of_date: Optional[readers.DateLike],
    stat: str,
) -> tuple[str, Any]:
    """The cut and date to draw: the ones given, else the only cut and latest date."""
    rows = readers.crosstabs(pool, model_id, parameter, stat, _ALL, as_of_date)
    if not rows:
        at = f" at {parameter}" if parameter else ""
        on = f" on {as_of_date}" if as_of_date is not None else ""
        raise NothingToDraw(
            f"No crosstabs are persisted for model {model_id}{at}{on} — run:"
            f" {_crosstabs_command(model_id, parameter)}"
        )
    cut = parameter or only_cut(
        sorted({r["parameter"] for r in rows}), f"Model {model_id}'s crosstabs"
    )
    day = (
        as_date(as_of_date)
        if as_of_date is not None
        else max(r["as_of_date"] for r in rows if r["parameter"] == cut)
    )
    return cut, day


def crosstabs(
    pool: DictRowPool,
    model_id: int,
    parameter: Optional[str] = None,
    as_of_date: Optional[readers.DateLike] = None,
    stat: str = "mean",
    compare: Optional[int] = None,
    top: int = 15,
    title: Optional[str] = None,
    features: Sequence[str] = (),
) -> PlotResult:
    """The features that most distinguish a model's top-k list from the rest.

    ``stat='mean'`` draws the ratio of the list's mean to the rest's on a log axis,
    labelled with both means. ``stat='nonzero_rate'`` draws the share of entities with a
    nonzero value on the list and off it: near 1 on the list and near 0 off it is the
    leakage signature, and a missing-value flag concentrated on the list is a
    data-quality signal. With ``compare``, the second model's list is read for the
    same features at the same cut and date.

    Args:
        pool: the project database.
        model_id: the model.
        parameter: the cut; ``None`` reads the only cut persisted.
        as_of_date: the prediction date; ``None`` reads the latest one persisted.
        stat: ``'mean'`` or ``'nonzero_rate'``.
        compare: a second model, drawn for the first model's features.
        top: the features drawn, the most distinguishing first.
        title: replaces the default title.
        features: draw only these features (exact names), in the ranking's order;
            ``top`` does not apply. Empty draws the ``top`` features.

    Returns:
        ``data`` maps ``'model <id>'`` to the :func:`readers.crosstabs` rows drawn for
        that model, in the first model's order.

    Raises:
        ValueError: ``stat`` is unknown, several cuts are persisted and ``parameter``
            is ``None``, or a feature in ``features`` has no crosstab.
        NothingToDraw: no crosstab is persisted for a model at the cut and date.
    """
    if compare == model_id:
        raise ValueError(
            f"compare={compare} is model {model_id} itself — compare it with another model"
        )
    _check_stat(stat)
    cut, day = _resolve(pool, model_id, parameter, as_of_date, stat)
    if features:
        ranking = readers.crosstabs(pool, model_id, cut, stat, _ALL, day)
        wanted = set(features)
        known = {r["feature"] for r in ranking}
        unknown = [f for f in dict.fromkeys(features) if f not in known]
        if unknown:
            raise ValueError(
                f"Not in model {model_id}'s crosstabs at {cut} on {day}:"
                f" {', '.join(unknown)} — give the feature names exactly as"
                " triage.crosstabs stores them"
            )
        rows_a = [r for r in ranking if r["feature"] in wanted]
    else:
        rows_a = readers.crosstabs(pool, model_id, cut, stat, top, day)
    data: dict[str, Any] = {f"model {model_id}": rows_a}
    missing: list[str] = []
    if compare is not None:
        other = {
            r["feature"]: r
            for r in readers.crosstabs(pool, compare, cut, stat, _ALL, day)
        }
        if not other:
            raise NothingToDraw(
                f"No crosstabs are persisted for model {compare} at {cut} on {day} — run:"
                f" {_crosstabs_command(compare, cut)}"
            )
        missing = [r["feature"] for r in rows_a if r["feature"] not in other]
        rows_a = [r for r in rows_a if r["feature"] in other]
        data = {
            f"model {model_id}": rows_a,
            f"model {compare}": [other[r["feature"]] for r in rows_a],
        }
    features = [r["feature"] for r in rows_a]

    with style():
        fig, axes = new_figure(8.4, 1.4 + 0.3 * len(features) * (1.4 if compare else 1))
        ax = axes[0, 0]
        ypos = list(range(len(features)))[::-1]  # the most distinguishing on top
        series = list(data.items())
        if stat == "mean":
            _draw_ratios(ax, ypos, series)
        else:
            _draw_nonzero(ax, ypos, series)
        ax.set_yticks(ypos, features, fontsize=7.5)
        ax.set_ylim(-0.7, max(len(features), 1) - 0.3)
        no_grid(ax, "y")
        models = " and ".join(name for name, _ in series)
        ax.set_title(title or f"Who is on the list: {models} at {day}, top {cut}")

    best = rows_a[0] if rows_a else None
    if best is None:
        lead = "no feature to draw"
    elif stat == "mean":
        lead = f"first {best['feature']} at {best['ratio']:.3g}×"
    else:
        lead = (
            f"first {best['feature']} at {best['selected_value']:.2f} on the list vs"
            f" {best['rest_value']:.2f}"
        )
    summary = (
        f"{', '.join(name for name, _ in series)} at {day}, {cut}:"
        f" {len(features)} features by {_STAT_NAME[stat]}; {lead}"
    )
    if missing:
        summary += (
            f"; {len(missing)} of model {model_id}'s top {len(missing) + len(features)}"
            f" left out, not in model {compare}'s crosstabs: {', '.join(missing)}"
        )
    return PlotResult(fig, data, summary)


def _draw_ratios(
    ax: Any, ypos: list[int], series: list[tuple[str, list[dict[str, Any]]]]
) -> None:
    """Bars of log10(ratio); one model labels each bar with its two means."""
    h = 0.6 if len(series) == 1 else 0.36
    offsets = [0.0] if len(series) == 1 else [h / 2, -h / 2]
    logs: list[float] = []
    for (name, rows), colour, off in zip(series, (BLUE, ORANGE), offsets, strict=False):
        values = [math.log10(r["ratio"]) for r in rows]
        logs += values
        ax.barh(
            [y + off for y in ypos],
            values,
            height=h,
            color=colour,
            label=name if len(series) > 1 else None,
        )
        if len(series) == 1:
            for y, r, x in zip(ypos, rows, values, strict=True):
                ax.annotate(
                    f"{r['selected_value']:.3g} vs {r['rest_value']:.3g}",
                    (x, float(y)),
                    xytext=(4 if x >= 0 else -4, 0),
                    textcoords="offset points",
                    ha="left" if x >= 0 else "right",
                    va="center",
                    fontsize=7.5,
                )
    log_ratio_axis(ax)
    lo, hi = min(logs + [-0.3]), max(logs + [0.3])
    pad = 0.9 if len(series) == 1 else 0.2
    ax.set_xlim(lo - pad, hi + pad)
    if len(series) == 1:
        ax.set_xlabel(
            "mean on the list ÷ mean on the rest (log scale; label: both means)"
        )
    else:
        ax.set_xlabel("mean on the list ÷ mean on the rest (log scale)")
        ax.legend(loc="lower right", fontsize=7.5)


def _draw_nonzero(
    ax: Any, ypos: list[int], series: list[tuple[str, list[dict[str, Any]]]]
) -> None:
    """A dumbbell per feature: the share nonzero on the list and on the rest."""
    if len(series) == 1:
        rows = series[0][1]
        for y, r in zip(ypos, rows, strict=True):
            ax.plot(
                [r["rest_value"], r["selected_value"]],
                [y, y],
                color=INK,
                linewidth=1.0,
                alpha=0.6,
            )
        for key, colour, label in (
            ("selected_value", BLUE, "on the list"),
            ("rest_value", ORANGE, "the rest"),
        ):
            ax.scatter(
                [r[key] for r in rows],
                ypos,
                s=36,
                color=colour,
                edgecolors="none",
                label=label,
                zorder=3,
            )
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
    else:
        handles: list[Line2D] = []
        labels: list[str] = []
        for (name, rows), colour, off in zip(
            series, (BLUE, ORANGE), (0.18, -0.18), strict=False
        ):
            ys = [y + off for y in ypos]
            for y, r in zip(ys, rows, strict=True):
                ax.plot(
                    [r["rest_value"], r["selected_value"]],
                    [y, y],
                    color=colour,
                    linewidth=1.0,
                    alpha=0.6,
                )
            ax.scatter(
                [r["selected_value"] for r in rows], ys, s=32, color=colour, zorder=3
            )
            ax.scatter(
                [r["rest_value"] for r in rows],
                ys,
                s=32,
                facecolors="none",
                edgecolors=colour,
                linewidths=1.2,
                zorder=3,
            )
            handles += [
                Line2D([], [], marker="o", linestyle="", color=colour),
                Line2D(
                    [],
                    [],
                    marker="o",
                    linestyle="",
                    markerfacecolor="none",
                    color=colour,
                ),
            ]
            labels += [f"{name}: on the list", f"{name}: the rest"]
        ax.legend(
            handles, labels, loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=7.5
        )
    ax.set_xlim(-0.02, 1.03)
    pct_axis(ax)
    ax.set_xlabel("share of entities with a nonzero value")


def crosstabs_dates(
    pool: DictRowPool,
    model_group_id: int,
    parameter: Optional[str] = None,
    stat: str = "mean",
    top: int = 4,
    title: Optional[str] = None,
) -> PlotResult:
    """The same crosstab on each of a model group's test dates.

    The features are those the group's latest list separates most; each line is one
    feature's mean ratio (``stat='mean'``) or its nonzero rate on the list minus the
    rest (``stat='nonzero_rate'``) on each (model, test date) with crosstabs.

    Args:
        pool: the project database.
        model_group_id: the model group.
        parameter: the cut; ``None`` reads the only cut persisted.
        stat: ``'mean'`` or ``'nonzero_rate'``.
        top: the features drawn (at most eight).
        title: replaces the default title.

    Returns:
        ``data`` maps each feature to one entry per (model, date):
        ``{model_id, as_of_date, row}``, where ``row`` is the :func:`readers.crosstabs`
        row of that feature, or ``None`` when that date has none.

    Raises:
        NotFoundError: the model group is unknown.
        ValueError: ``stat`` is unknown, ``top`` is above eight, or several cuts are
            persisted and ``parameter`` is ``None``.
        NothingToDraw: no crosstab is persisted for the group's models.
    """
    _check_stat(stat)
    check_series(top, "features", "pass a smaller --top")
    models = readers.group_models(pool, model_group_id)
    if not models:
        raise readers.NotFoundError(
            f"model group {model_group_id} not found — list a project's groups with:"
            " triage models <experiment>"
        )
    by_model = {
        m["model_id"]: readers.crosstabs(pool, m["model_id"], parameter, stat, _ALL)
        for m in models
    }
    every = [r for rows in by_model.values() for r in rows]
    if not every:
        ids = ", ".join(str(m["model_id"]) for m in models)
        raise NothingToDraw(
            f"No crosstabs are persisted for model group {model_group_id}'s models"
            f" ({ids}) — run, for each: {_crosstabs_command('<model_id>', parameter)}"
        )
    cut = parameter or only_cut(
        sorted({r["parameter"] for r in every}),
        f"Model group {model_group_id}'s crosstabs",
    )
    pairs = sorted(
        {
            (r["as_of_date"], m)
            for m, rows in by_model.items()
            for r in rows
            if r["parameter"] == cut
        }
    )
    latest_date, latest_model = pairs[-1]
    features = [
        r["feature"]
        for r in by_model[latest_model]
        if r["parameter"] == cut and r["as_of_date"] == latest_date
    ][:top]
    index = {
        (m, r["as_of_date"], r["feature"]): r
        for m, rows in by_model.items()
        for r in rows
        if r["parameter"] == cut
    }
    data: dict[str, Any] = {
        f: [
            {"model_id": m, "as_of_date": d, "row": index.get((m, d, f))}
            for d, m in pairs
        ]
        for f in features
    }

    def value(row: Optional[dict[str, Any]]) -> float:
        if row is None:
            return float("nan")
        if stat == "mean":
            return nan(row["ratio"])
        return nan(row["selected_value"]) - nan(row["rest_value"])

    with style():
        fig, axes = new_figure(8.6, 3.6)
        ax = axes[0, 0]
        xs = list(range(len(pairs)))
        for f, colour in zip(features, SERIES, strict=False):
            ax.plot(
                xs,
                [value(e["row"]) for e in data[f]],
                color=colour,
                marker="o",
                markersize=4,
                label=f,
            )
        ax.set_xticks(xs, [f"{d}\nmodel {m}" for d, m in pairs], fontsize=7.5)
        if stat == "mean":
            ax.axhline(1.0, color=INK, linewidth=0.8)
            ax.set_ylim(bottom=0)
            ax.set_ylabel("mean ratio, list ÷ rest")
        else:
            ax.axhline(0.0, color=INK, linewidth=0.8)
            pct_axis(ax, "y")
            ax.set_ylabel("nonzero rate, list − rest")
        ax.set_title(
            title
            or f"The same crosstab across test dates — model group {model_group_id},"
            f" top {cut}"
        )
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=7.5)

    summary = (
        f"{len(features)} feature(s) by {_STAT_NAME[stat]} across {len(pairs)} test"
        f" date(s) of model group {model_group_id} at {cut}"
    )
    return PlotResult(fig, data, summary)
