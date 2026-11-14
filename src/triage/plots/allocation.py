"""How much of the outcome a model's top k hold: the cumulative-gains curve and its lift.

The gains curve reads the population ranking of one scored date (migration 0035): x is the
share of the scored rows selected, y the share of the labeled outcome they hold. The dashed
diagonal is a random ranking. Lift, the top-k mean outcome over the population's, is drawn on
a second axis.
"""

from __future__ import annotations

from typing import Optional

from triage import problem_types
from triage.diagnostics import readers
from triage.plots.style import (
    BASE_DASH,
    BLUE,
    INK,
    ORANGE,
    NothingToDraw,
    PlotResult,
    nan,
    new_figure,
    pct_axis,
    style,
)
from triage.util.db import DictRowPool

#: The cut the summary reads the curve at.
SUMMARY_SHARE = 0.10


def gains(
    pool: DictRowPool,
    model_id: int,
    as_of_date: Optional[str] = None,
    points: int = 100,
    title: Optional[str] = None,
) -> PlotResult:
    """The cumulative-gains curve of one test date, with the diagonal and the lift.

    Args:
        pool: the project database.
        model_id: a classification, regression or regression_ranking model.
        as_of_date: the test date; ``None`` reads the latest evaluated one.
        points: the cuts after k = 0.
        title: replaces the default title.

    Returns:
        ``data`` holds the :func:`readers.gains_curve` result.

    Raises:
        WrongProblemType: the model is a survival model.
        NotFoundError: no model has this id.
        NothingToDraw: the model has no evaluated test date, no labeled row on it, or a
            labeled outcome that is negative or sums to 0 (a share is then undefined).
    """
    problem_types.require(pool, "plot gains", model_id)
    result = readers.gains_curve(pool, model_id, as_of_date=as_of_date, points=points)
    if result is None or not result["rows"]:
        raise NothingToDraw(
            f"Model {model_id} has no scored test rows on an evaluated date. Score and"
            " evaluate it first: triage run <config>."
        )
    rows = result["rows"]
    if all(r["captured"] is None for r in rows):
        raise NothingToDraw(
            f"Model {model_id}'s labeled outcomes on {result['as_of_date']} are negative"
            " somewhere or sum to 0, so the share of the outcome the top k hold is"
            " undefined. Read its errors instead: triage plot residuals"
            f" {model_id}."
        )

    xs = [r["k_pct"] for r in rows]
    with style():
        fig, axes = new_figure(5.6, 4.2)
        ax = axes[0, 0]
        ax.plot([0, 1], [0, 1], color=INK, linestyle=BASE_DASH, linewidth=1.0)
        ax.plot(xs, [nan(r["captured"]) for r in rows], color=BLUE, label="captured")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1.02)
        pct_axis(ax, "x")
        pct_axis(ax, "y")
        ax.set_xlabel("share of the scored rows selected (top k)")
        ax.set_ylabel("share of the outcome captured", color=BLUE)
        lift_ax = ax.twinx()
        lift_ax.plot(
            xs[1:], [nan(r["lift"]) for r in rows[1:]], color=ORANGE, label="lift"
        )
        lift_ax.axhline(1, color=ORANGE, linestyle=BASE_DASH, linewidth=0.8)
        lift_ax.set_ylabel("lift (top-k mean outcome ÷ mean)", color=ORANGE)
        lift_ax.spines["right"].set_visible(True)
        lift_ax.grid(visible=False)
        ax.set_title(
            title
            or f"How much does model {model_id}'s top k hold? ({result['as_of_date']})"
        )

    at = min(rows, key=lambda r: abs(r["k_pct"] - SUMMARY_SHARE))
    summary = (
        f"{result['as_of_date']} ({result['problem_type']}): the top"
        f" {at['k_pct']:.0%} ({at['k']} rows) hold {nan(at['captured']):.1%}"
        f" of the outcome, lift {nan(at['lift']):.2f}"
    )
    if result["aucc"] is not None:
        summary += f"; AUCC {result['aucc']:.4f}"
    return PlotResult(fig, {"gains": result}, summary)
