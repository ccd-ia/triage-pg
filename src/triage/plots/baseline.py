"""A regression model group against its baseline: per segment, and its wins over time."""

from __future__ import annotations

from typing import Optional

from triage.diagnostics.baseline_compare import compare_to_baseline
from triage.plots.style import (
    BLUE,
    INK,
    ORANGE,
    NothingToDraw,
    PlotResult,
    new_figure,
    no_grid,
    style,
)
from triage.util.db import DictRowPool


def segments(
    pool: DictRowPool,
    model_group_id: int,
    baseline_group_id: Optional[int] = None,
    cut: str = "all",
    title: Optional[str] = None,
) -> PlotResult:
    """MAE per segment, the model and the baseline as paired dots on one row each.

    Args:
        pool: the project database.
        model_group_id: the model group.
        baseline_group_id: the baseline; ``None`` picks the default baseline.
        cut: the segments, as ``triage postmodel baseline --cut`` takes them.
        title: replaces the default title.

    Returns:
        ``data`` holds ``'segments'``, the comparison's segment rows.

    Raises:
        ComparisonError: the groups cannot be compared.
    """
    result = compare_to_baseline(pool, model_group_id, baseline_group_id, cut=cut)
    rows = result.segments
    labels = [f"{r['cut']}: {r['segment']}" for r in rows]
    ys = list(range(len(rows)))[::-1]
    with style():
        fig, axes = new_figure(6.4, 0.32 * len(rows) + 1.4)
        ax = axes[0, 0]
        for y, r in zip(ys, rows, strict=True):
            ax.plot([r["mae_m"], r["mae_b"]], [y, y], color=INK, linewidth=0.8)
        ax.scatter([r["mae_b"] for r in rows], ys, color=ORANGE, s=22,
                   label=f"baseline (group {result.baseline_group_id})", zorder=3)  # fmt: skip
        ax.scatter([r["mae_m"] for r in rows], ys, color=BLUE, s=22,
                   label=f"model (group {model_group_id})", zorder=3)  # fmt: skip
        ax.set_yticks(ys, labels)
        ax.set_xlabel("MAE on the rows both scored")
        ax.legend(loc="lower right")
        no_grid(ax, "y")
        ax.set_title(
            title
            or f"Where does group {model_group_id} beat group {result.baseline_group_id}?"
        )
    won = sum(r["model_wins"] for r in rows)
    summary = (
        f"group {model_group_id} against {result.baseline_group_id}: wins {won} of"
        f" {len(rows)} segments over {len(result.dates)} test date(s)"
    )
    return PlotResult(fig, {"segments": rows}, summary)


def win_consistency(
    pool: DictRowPool,
    model_group_id: int,
    baseline_group_id: Optional[int] = None,
    title: Optional[str] = None,
) -> PlotResult:
    """Entities by the number of test dates the model won, against the count by chance.

    Args:
        pool: the project database.
        model_group_id: the model group.
        baseline_group_id: the baseline; ``None`` picks the default baseline.
        title: replaces the default title.

    Returns:
        ``data`` holds ``'chance'``, the comparison's chance test.

    Raises:
        ComparisonError: the groups cannot be compared.
        NothingToDraw: they share fewer than two test dates.
    """
    result = compare_to_baseline(pool, model_group_id, baseline_group_id, cut="band")
    chance = result.chance
    if chance is None:
        raise NothingToDraw(
            f"Groups {model_group_id} and {result.baseline_group_id} share fewer than two"
            " test dates, or no entity is scored on all of them — the counts need two."
            " Score more test dates: triage run <config>."
        )
    ks = list(range(len(chance["observed"])))
    with style():
        fig, axes = new_figure(5.6, 3.6)
        ax = axes[0, 0]
        ax.bar(ks, chance["observed"], color=BLUE, label="observed")
        ax.plot(ks, chance["expected"], color=ORANGE, marker="o", markersize=4,
                label="expected by chance")  # fmt: skip
        ax.set_xticks(ks)
        ax.set_xlabel(f"test dates won, of {chance['n_dates']}")
        ax.set_ylabel("entities")
        ax.legend(loc="upper right")
        no_grid(ax, "x")
        ax.set_title(
            title or f"Does group {model_group_id} win on the same entities every date?"
        )
    p_value = chance["p_value"]
    summary = (
        f"{chance['n_entities']} entities on {chance['n_dates']} dates; chi-square"
        + (" not defined (one cell)" if p_value is None else f" p = {p_value:.3g}")
    )
    return PlotResult(fig, {"chance": chance}, summary)
