"""One model's scores: their calibration against the outcome, and how they spread."""

from __future__ import annotations

from typing import Optional

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
    no_grid,
    span,
    style,
)
from triage.util.db import DictRowPool


def calibration(
    pool: DictRowPool,
    model_id: int,
    bins: int = 10,
    title: Optional[str] = None,
) -> PlotResult:
    """Mean score against what the rows of each score bucket realized, on one test date.

    The y axis is the realized rate for a classifier and the mean outcome for the other
    types. The dashed diagonal is score = outcome: a calibrated model's points sit on
    it. Tied scores share a bucket, so a model that gives most rows one score draws
    fewer points than ``bins``; the summary names the largest tie.

    Args:
        pool: the project database.
        model_id: the model.
        bins: the number of score buckets.
        title: replaces the default title.

    Returns:
        ``data`` holds ``'deciles'`` (the :func:`readers.calibration` rows) and
        ``'ties'`` (its tie summary).

    Raises:
        NotFoundError: no model has this id.
        ValueError: ``bins`` is below 1.
        NothingToDraw: the model has no evaluated test date, or no labeled row on it.
    """
    result = readers.calibration(pool, model_id, bins=bins)
    if result is None or not result["deciles"]:
        raise NothingToDraw(
            f"Model {model_id} has no labeled test rows on an evaluated date — calibration"
            " is drawn over the latest evaluated test date. Score and evaluate the model"
            " first: triage run <config>."
        )
    rows = result["deciles"]
    classifier = result["problem_type"] == "classification"
    y_key = "realized_rate" if classifier else "mean_outcome"
    y_label = "realized rate" if classifier else "mean outcome"
    ties = result["ties"]

    with style():
        fig, axes = new_figure(5.6, 4.2)
        ax = axes[0, 0]
        xs = [nan(r["avg_score"]) for r in rows]
        ys = [nan(r[y_key]) for r in rows]
        defined = [v for v in xs + ys if v == v]  # nan != nan: an undefined value
        lo, hi = min(defined), max(defined)
        ax.plot([lo, hi], [lo, hi], color=INK, linestyle=BASE_DASH, linewidth=1.0)
        ax.plot(xs, ys, color=BLUE, marker="o", markersize=4)
        for r, x, y in zip(rows, xs, ys, strict=True):
            ax.annotate(
                str(r["decile"]),
                (x, y),
                xytext=(4, 3),
                textcoords="offset points",
                fontsize=7.5,
            )
        ax.set_xlabel("mean score of the bucket")
        ax.set_ylabel(y_label)
        ax.set_title(
            title
            or f"Do model {model_id}'s scores match its outcomes? ({result['as_of_date']})"
        )

    summary = (
        f"{len(rows)} of {bins} buckets at {result['as_of_date']}"
        f" ({result['problem_type']}): {y_label} {span([r[y_key] for r in rows])},"
        f" lift {span([r['lift'] for r in rows])}"
    )
    if ties is not None and ties["largest_tie_share"] is not None:
        summary += (
            f"; largest tie {ties['largest_tie_share']:.0%} of rows"
            f" at score {ties['largest_tie_score']:.4g}"
        )
    return PlotResult(fig, {"deciles": rows, "ties": ties}, summary)


def scores(
    pool: DictRowPool,
    model_id: int,
    bins: int = 20,
    title: Optional[str] = None,
) -> PlotResult:
    """How a model's test scores spread over every test date.

    A classifier whose scores lie in ``[0, 1]`` is binned over ``[0, 1]`` and draws its
    positives over all rows; any other model is binned over the range of its own scores
    and draws one series.

    Args:
        pool: the project database.
        model_id: the model.
        bins: the number of equal-width buckets.
        title: replaces the default title.

    Returns:
        ``data`` holds ``'bins'``, the :func:`readers.score_histogram` rows.

    Raises:
        ValueError: ``bins`` is below 1.
        NothingToDraw: the model has no test predictions.
    """
    rows = readers.score_histogram(pool, model_id, bins)
    if not rows:
        raise NothingToDraw(
            f"Model {model_id} has no test predictions — the histogram is drawn over its"
            " test scores. Score the model first: triage run <config>."
        )
    has_pos = any(r["n_pos"] is not None for r in rows)

    with style():
        fig, axes = new_figure(6.4, 3.4)
        ax = axes[0, 0]
        lefts = [float(r["lo"]) for r in rows]
        # one score for every row gives a zero-width bin: draw it at a visible width
        widths = [(float(r["hi"]) - float(r["lo"])) or 1.0 for r in rows]
        ax.bar(
            lefts,
            [r["n"] for r in rows],
            width=widths,
            align="edge",
            color=BLUE,
            label="all rows",
        )
        if has_pos:
            ax.bar(
                lefts,
                [r["n_pos"] or 0 for r in rows],
                width=widths,
                align="edge",
                color=ORANGE,
                label="positives",
            )
            ax.legend(loc="upper right")
        ax.set_xlabel("score")
        ax.set_ylabel("rows")
        ax.set_title(title or f"How do model {model_id}'s test scores spread?")
        no_grid(ax, "x")

    total = sum(r["n"] for r in rows)
    fullest = max(rows, key=lambda r: (r["n"], -r["bin"]))
    summary = (
        f"{total} scored rows over {rows[0]['lo']:.4g}–{rows[-1]['hi']:.4g}"
        f" in {len(rows)} of {bins} bins; the fullest holds {fullest['n']}"
        f" ({fullest['n'] / total:.0%}) at {fullest['lo']:.4g}–{fullest['hi']:.4g}"
    )
    if has_pos:
        summary += f"; {sum(r['n_pos'] or 0 for r in rows)} positives"
    return PlotResult(fig, {"bins": rows}, summary)
