"""List overlap: do two model groups flag the same entities on each test date?

:func:`overlap` reads one cut. :func:`overlap_sweep` reads several and draws the Jaccard
index against k, which shows whether two lists agree at the top and part further down, or
the other way round.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from triage.diagnostics import readers
from triage.plots.style import (
    BLUE,
    INK,
    ORANGE,
    SERIES,
    NothingToDraw,
    PlotResult,
    check_series,
    nan,
    new_figure,
    no_grid,
    span,
    style,
)
from triage.util.db import DictRowPool


def _models(pool: DictRowPool, model_group_id: int) -> list[dict[str, Any]]:
    models = readers.group_models(pool, model_group_id)
    if not models:
        raise readers.NotFoundError(
            f"model group {model_group_id} not found — list a project's groups with:"
            " triage models <experiment>"
        )
    return models


def _pairs(pool: DictRowPool, group_a: int, group_b: int) -> list[tuple[int, int]]:
    """Each model of ``group_a`` with the model of ``group_b`` trained up to its date."""
    models_a = _models(pool, group_a)
    peers = {m["train_end_time"]: m["model_id"] for m in _models(pool, group_b)}
    return [
        (m["model_id"], peers[m["train_end_time"]])
        for m in models_a
        if m["train_end_time"] in peers
    ]


def _no_shared_date(group_a: int, group_b: int) -> NothingToDraw:
    return NothingToDraw(
        f"Model groups {group_a} and {group_b} share no test date: no two of their"
        " models were trained on the same window and scored the same date. Score"
        " both groups' models first (triage run <config>)."
    )


def overlap(
    pool: DictRowPool,
    group_a: int,
    group_b: int,
    parameter: str,
    title: Optional[str] = None,
) -> PlotResult:
    """The Jaccard index of two model groups' top-k lists, and the rank correlation of
    their scores, on each test date both scored.

    The models are paired by training window: each model of ``group_a`` with the model
    of ``group_b`` trained up to the same date, which scores the same test dates. Each
    model's cut is taken over its own population.

    Args:
        pool: the project database.
        group_a: the first model group.
        group_b: the second model group.
        parameter: the top-k cut.
        title: replaces the default title.

    Returns:
        ``data`` maps ``'model <a> vs model <b>'`` to the :func:`readers.list_overlap`
        rows of that pair, one per shared test date.

    Raises:
        NotFoundError: a model group is unknown.
        NothingToDraw: no pair of models shares a test date.
    """
    data: dict[str, Any] = {}
    for model, peer in _pairs(pool, group_a, group_b):
        rows = readers.list_overlap(pool, model, peer, parameter, "test")
        if rows:
            data[f"model {model} vs model {peer}"] = rows
    if not data:
        raise _no_shared_date(group_a, group_b)
    points = [(pair, r) for pair, rows in data.items() for r in rows]

    with style():
        # the legend sits right of the axes: keep room for both with one date
        fig, axes = new_figure(max(6.0, 3.6 + 1.1 * len(points)), 3.2)
        ax = axes[0, 0]
        xs = list(range(len(points)))
        h = 0.36
        jaccard = [nan(r["jaccard"]) for _, r in points]
        rank_corr = [nan(r["rank_corr"]) for _, r in points]
        ax.bar([x - h / 2 for x in xs], jaccard, width=h, color=BLUE, label="Jaccard")
        ax.bar(
            [x + h / 2 for x in xs],
            rank_corr,
            width=h,
            color=ORANGE,
            label="rank correlation (Spearman)",
        )
        for x, (_, r) in zip(xs, points, strict=True):
            for value, at in ((r["jaccard"], x - h / 2), (r["rank_corr"], x + h / 2)):
                if value is not None:
                    ax.annotate(
                        f"{value:.2f}",
                        (at, value),
                        xytext=(0, 2),
                        textcoords="offset points",
                        ha="center",
                        fontsize=7.5,
                    )
        ax.axhline(0, color=INK, linewidth=0.6)
        ax.set_xticks(
            xs,
            [f"{r['as_of_date']}\n{pair.replace('model ', '')}" for pair, r in points],
            fontsize=7.5,
        )
        defined = [v for v in rank_corr if v == v]  # nan != nan: an undefined value
        ax.set_ylim(min([0.0] + [v - 0.1 for v in defined]), 1.1)
        ax.set_xlabel("test date, and the two models compared")
        ax.set_title(
            title
            or f"Do model groups {group_a} and {group_b} flag the same entities?"
            f" (top {parameter})"
        )
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
        no_grid(ax, "x")

    rows = [r for _, r in points]
    summary = (
        f"{len(rows)} shared test date(s) at {parameter}: Jaccard"
        f" {span([r['jaccard'] for r in rows])}, rank correlation"
        f" {span([r['rank_corr'] for r in rows])}"
    )
    return PlotResult(fig, data, summary)


def overlap_sweep(
    pool: DictRowPool,
    group_a: int,
    group_b: int,
    parameters: Sequence[str],
    title: Optional[str] = None,
) -> PlotResult:
    """The Jaccard index of two model groups' top-k lists against k, over several cuts.

    The models are paired by training window, as :func:`overlap` pairs them. Each pair
    and test date is one line; its x is the first model's k at each cut, resolved over
    its own population.

    Args:
        pool: the project database.
        group_a: the first model group.
        group_b: the second model group.
        parameters: the top-k cuts, e.g. ``['50_abs', '100_abs', '200_abs']``.
        title: replaces the default title.

    Returns:
        ``data`` maps ``'model <a> vs model <b>'`` to the
        :func:`readers.list_overlap_sweep` rows of that pair.

    Raises:
        NotFoundError: a model group is unknown.
        ValueError: the cuts are empty or repeated, or there are more lines than one
            figure draws.
        NothingToDraw: no pair of models shares a test date.
    """
    data: dict[str, Any] = {}
    for model, peer in _pairs(pool, group_a, group_b):
        rows = readers.list_overlap_sweep(pool, model, peer, parameters, "test")
        if rows:
            data[f"model {model} vs model {peer}"] = rows
    if not data:
        raise _no_shared_date(group_a, group_b)
    lines: dict[str, list[dict[str, Any]]] = {}
    for pair, rows in data.items():
        for r in rows:
            label = f"{r['as_of_date']}: {pair.replace('model ', '')}"
            lines.setdefault(label, []).append(r)
    check_series(
        len(lines),
        "pairs of models and test dates",
        "compare two groups with fewer shared test dates",
    )

    with style():
        fig, axes = new_figure(7.0, 3.6)
        ax = axes[0, 0]
        for color, (label, rows) in zip(SERIES, lines.items(), strict=False):
            points = sorted(rows, key=lambda r: r["k_a"])
            ax.plot(
                [r["k_a"] for r in points],
                [nan(r["jaccard"]) for r in points],
                marker="o",
                color=color,
                label=label,
            )
        ax.set_ylim(0, 1.05)
        ax.set_xlabel("k (the first group's model)")
        ax.set_ylabel("Jaccard index of the two top-k lists")
        ax.set_title(
            title
            or f"How model groups {group_a} and {group_b} agree as k grows"
            f" ({', '.join(parameters)})"
        )
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=7.5)

    rows = [r for rows in data.values() for r in rows]
    by_cut = {
        cut: [r["jaccard"] for r in rows if r["parameter"] == cut] for cut in parameters
    }
    summary = (
        f"{len(lines)} pair(s) of models and test dates over {len(parameters)} cut(s):"
        + "; ".join(f" Jaccard {span(v)} at {cut}" for cut, v in by_cut.items())
    )
    return PlotResult(fig, data, summary)
