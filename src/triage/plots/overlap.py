"""List overlap: do two model groups flag the same entities on each test date?"""

from __future__ import annotations

from typing import Any, Optional

from triage.diagnostics import readers
from triage.plots.style import (
    BLUE,
    INK,
    ORANGE,
    NothingToDraw,
    PlotResult,
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
    models_a = _models(pool, group_a)
    peers = {m["train_end_time"]: m["model_id"] for m in _models(pool, group_b)}
    data: dict[str, Any] = {}
    for m in models_a:
        peer = peers.get(m["train_end_time"])
        if peer is None:
            continue
        rows = readers.list_overlap(pool, m["model_id"], peer, parameter, "test")
        if rows:
            data[f"model {m['model_id']} vs model {peer}"] = rows
    if not data:
        raise NothingToDraw(
            f"Model groups {group_a} and {group_b} share no test date: no two of their"
            " models were trained on the same window and scored the same date. Score"
            " both groups' models first (triage run <config>)."
        )
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
