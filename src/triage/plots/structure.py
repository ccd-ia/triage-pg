"""A model group's feature importance by family, across its train ends."""

from __future__ import annotations

from typing import Optional

from triage.diagnostics.structure import importance_by_family
from triage.plots.style import (
    INK,
    MAX_SERIES,
    SERIES,
    NothingToDraw,
    PlotResult,
    new_figure,
    no_grid,
    pct_axis,
    style,
)
from triage.util.db import DictRowPool


def importance(
    pool: DictRowPool, model_group_id: int, title: Optional[str] = None
) -> PlotResult:
    """Each family's share of Σ|importance|, one stacked bar per model in training order.

    The families drawn are the seven with the largest mean share over the models; the rest
    are summed into one grey segment.

    Raises:
        NothingToDraw: the group has no feature importances.
    """
    result = importance_by_family(pool, model_group_id)
    models = result["models"]
    if not models:
        raise NothingToDraw(
            f"Model group {model_group_id} has no feature importances — its estimators"
            " expose none, or it was never trained: triage run <config>."
        )
    totals: dict[str, float] = {}
    for m in models:
        for fam, share in m["families"].items():
            totals[fam] = totals.get(fam, 0.0) + share / len(models)
    shown = sorted(totals, key=lambda f: (-totals[f], f))[: MAX_SERIES - 1]
    labels = [str(m["train_end_time"]) for m in models]
    with style():
        fig, axes = new_figure(max(5.2, 0.6 * len(models) + 3.4), 3.8)
        ax = axes[0, 0]
        bottom = [0.0] * len(models)
        for colour, fam in zip(SERIES, shown, strict=False):
            heights = [m["families"].get(fam, 0.0) for m in models]
            ax.bar(labels, heights, bottom=bottom, color=colour, label=fam)
            bottom = [b + h for b, h in zip(bottom, heights, strict=True)]
        rest = [1.0 - b for b in bottom]
        if any(r > 1e-9 for r in rest):
            ax.bar(labels, rest, bottom=bottom, color=INK, alpha=0.35, label="other")
        pct_axis(ax, "y")
        ax.set_ylabel("share of |importance|")
        ax.set_xlabel("train end")
        ax.tick_params(axis="x", labelrotation=45)
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
        no_grid(ax, "x")
        ax.set_title(
            title or f"Which source columns does model group {model_group_id} lean on?"
        )
    lead = shown[0]
    summary = (
        f"{len(models)} model(s), {len(totals)} families; {lead} holds a mean"
        f" {totals[lead]:.0%} of the importance"
    )
    return PlotResult(fig, result, summary)
