"""A survival model's censoring-aware diagnostics, as ``triage postmodel survival`` persisted them.

* :func:`km` — the Kaplan-Meier curve of each risk tertile with the log-rank p-value, and the
  KM event probability by the horizon per risk decile.
* :func:`brier` — Brier(t) over the time grid, beside the Brier of the training KM (a model
  that knows nothing of the features).
* :func:`survival_calibration` — predicted against KM-observed survival at the horizon per
  decile of predicted survival, with the diagonal, ICI and E50.

Each reads :func:`triage.diagnostics.readers.survival_diagnostics` at one date and horizon:
the latest date at the horizon computed most recently, unless both are given.
"""

from __future__ import annotations

from typing import Any, Optional

from triage import problem_types
from triage.diagnostics import readers
from triage.plots.style import (
    AQUA,
    BASE_DASH,
    BLUE,
    INK,
    ORANGE,
    SERIES,
    NothingToDraw,
    PlotResult,
    fmt_rate,
    nan,
    new_figure,
    no_grid,
    pct_axis,
    span,
    style,
)
from triage.util.db import DictRowPool

#: The tertiles' names, riskiest first, as the curves are labeled.
GROUP_NAMES = {1: "highest risk", 2: "middle", 3: "lowest risk"}


def _read(
    pool: DictRowPool,
    command: str,
    model_id: int,
    horizon: Optional[float],
    as_of_date: Optional[str],
) -> dict[str, Any]:
    problem_types.require(pool, command, model_id)
    result = readers.survival_diagnostics(pool, model_id, horizon, as_of_date)
    if result is None:
        at = "" if horizon is None else f" at horizon {horizon:g}"
        on = "" if as_of_date is None else f" on {as_of_date}"
        raise NothingToDraw(
            f"Model {model_id} has no survival diagnostics{at}{on}. Compute them first:"
            f" triage postmodel survival {model_id} --horizon"
            f" {'H' if horizon is None else f'{horizon:g}'}."
        )
    return result


def _undefined(rows: list[dict[str, Any]]) -> Optional[str]:
    """The reason the rows' values are undefined, when none of them is defined."""
    if any(r["value"] is not None for r in rows):
        return None
    reasons = [r["extra"].get("undefined") for r in rows if r["extra"].get("undefined")]
    return reasons[0] if reasons else "no value"


def km(
    pool: DictRowPool,
    model_id: int,
    horizon: Optional[float] = None,
    as_of_date: Optional[str] = None,
    title: Optional[str] = None,
) -> PlotResult:
    """KM curves by risk tertile, and the KM event probability by the horizon per decile.

    Args:
        pool: the project database.
        model_id: a survival model.
        horizon: the horizon; ``None`` reads the one computed most recently.
        as_of_date: the test date; ``None`` reads the latest computed at the horizon.
        title: replaces the default title of the curves.

    Returns:
        ``data`` holds the :func:`readers.survival_diagnostics` result.

    Raises:
        WrongProblemType: the model is not a survival model.
        ValueError: no model has this id.
        NothingToDraw: nothing is persisted at that horizon and date.
    """
    result = _read(pool, "plot km", model_id, horizon, as_of_date)
    kinds = result["kinds"]
    h = float(result["horizon"])
    curves: dict[int, list[dict[str, Any]]] = {}
    for r in kinds["km_group"]:
        curves.setdefault(r["bin"], []).append(r)
    deciles = [r for r in kinds["km_decile"] if r["bin"] > 0]
    population = next((r for r in kinds["km_decile"] if r["bin"] == 0), None)
    logrank = kinds["logrank"][0] if kinds["logrank"] else None

    with style():
        fig, axes = new_figure(10.4, 4.0, 1, 2)
        ax = axes[0, 0]
        for g, rows in sorted(curves.items()):
            ts, ss = [r["t"] for r in rows], [nan(r["value"]) for r in rows]
            if g == 0:
                ax.step(ts, ss, where="post", color=INK, linestyle=BASE_DASH,
                        linewidth=1.0, label="all rows")  # fmt: skip
            else:
                ax.step(ts, ss, where="post", color=SERIES[g - 1],
                        label=f"{GROUP_NAMES.get(g, g)} (n={rows[0]['n']})")  # fmt: skip
        ax.axvline(h, color=INK, linewidth=0.8)
        ax.set_ylim(0, 1.02)
        pct_axis(ax, "y")
        ax.set_xlabel("duration")
        ax.set_ylabel("Kaplan-Meier survival")
        ax.legend(loc="lower left")
        p = None if logrank is None else logrank["value"]
        ax.set_title(
            title
            or f"Do model {model_id}'s risk groups separate? ({result['as_of_date']})"
        )
        if p is not None:
            ax.annotate(
                f"log-rank p = {p:.3g}",
                (0.98, 0.98),
                xycoords="axes fraction",
                ha="right",
                va="top",
                fontsize=8.5,
            )

        bars = axes[0, 1]
        xs = [str(r["bin"]) for r in deciles]
        bars.bar(xs, [nan(r["value"]) for r in deciles], color=BLUE)
        if population is not None and population["value"] is not None:
            bars.axhline(
                population["value"], color=ORANGE, linestyle=BASE_DASH, linewidth=1.0
            )
        pct_axis(bars, "y")
        bars.set_xlabel("risk decile (1 = highest scores)")
        bars.set_ylabel(f"KM probability of the event by {h:g}")
        bars.set_title(f"Event by the horizon {h:g}, per risk decile")
        no_grid(bars, "x")

    summary = (
        f"{result['as_of_date']} at horizon {h:g}: KM event probability"
        f" {span([r['value'] for r in deciles])} across {len(deciles)} deciles"
    )
    top = deciles[0] if deciles else None
    if (
        top is not None
        and top["value"] is not None
        and population is not None
        and population["value"] is not None
    ):
        summary += (
            f", decile {top['bin']} {top['value']:.1%} against"
            f" {population['value']:.1%} overall (lift"
            f" {fmt_rate(top['extra'].get('lift'))})"
        )
    if logrank is not None:
        summary += (
            f"; log-rank p = {logrank['value']:.3g} across the tertiles"
            if logrank["value"] is not None
            else f"; log-rank undefined ({logrank['extra'].get('undefined')})"
        )
    return PlotResult(fig, {"survival": result}, summary)


def brier(
    pool: DictRowPool,
    model_id: int,
    horizon: Optional[float] = None,
    as_of_date: Optional[str] = None,
    title: Optional[str] = None,
) -> PlotResult:
    """Brier(t) over the time grid, with the Brier of the training Kaplan-Meier.

    Args:
        pool: the project database.
        model_id: a survival model.
        horizon: the horizon the grid was computed with; ``None`` reads the latest.
        as_of_date: the test date; ``None`` reads the latest computed at the horizon.
        title: replaces the default title.

    Returns:
        ``data`` holds the :func:`readers.survival_diagnostics` result.

    Raises:
        WrongProblemType: the model is not a survival model.
        ValueError: no model has this id.
        NothingToDraw: nothing is persisted, or Brier(t) is undefined at every time (an
            estimator without ``predict_survival_function``, a grid outside the follow-up).
    """
    result = _read(pool, "plot brier", model_id, horizon, as_of_date)
    rows = result["kinds"]["brier"]
    why = _undefined(rows)
    if why is not None:
        raise NothingToDraw(
            f"Model {model_id}'s Brier(t) is undefined on {result['as_of_date']} at every"
            f" time of the grid: {why}. Read its ranking instead: triage plot km"
            f" {model_id}."
        )
    defined = [r for r in rows if r["value"] is not None]
    reference = [r["extra"].get("km_reference") for r in defined]

    with style():
        fig, axes = new_figure(5.6, 4.0)
        ax = axes[0, 0]
        ts = [r["t"] for r in defined]
        ax.plot(ts, [r["value"] for r in defined], color=BLUE, marker="o",
                markersize=4, label="model")  # fmt: skip
        if any(v is not None for v in reference):
            ax.plot(ts, [nan(v) for v in reference], color=INK, linestyle=BASE_DASH,
                    linewidth=1.0, label="training Kaplan-Meier")  # fmt: skip
        ax.axvline(float(result["horizon"]), color=INK, linewidth=0.8)
        ax.set_ylim(bottom=0)
        ax.set_xlabel("time t (label duration units)")
        ax.set_ylabel("Brier score at t (lower is better)")
        ax.legend(loc="upper left")
        ax.set_title(
            title
            or f"How far is model {model_id}'s S(t) from the outcome?"
            f" ({result['as_of_date']})"
        )

    best = min(defined, key=lambda r: (r["value"], r["t"]))
    summary = (
        f"{result['as_of_date']}: Brier {span([r['value'] for r in defined], 3)} over"
        f" t = {ts[0]:g}–{ts[-1]:g} ({len(defined)} of {len(rows)} times defined),"
        f" lowest {best['value']:.3f} at t = {best['t']:g}"
    )
    if any(v is not None for v in reference):
        summary += f"; training KM {span(reference, 3)}"
    return PlotResult(fig, {"survival": result}, summary)


def survival_calibration(
    pool: DictRowPool,
    model_id: int,
    horizon: Optional[float] = None,
    as_of_date: Optional[str] = None,
    title: Optional[str] = None,
) -> PlotResult:
    """Predicted against KM-observed survival at the horizon, per decile of prediction.

    Args:
        pool: the project database.
        model_id: a survival model.
        horizon: the horizon; ``None`` reads the one computed most recently.
        as_of_date: the test date; ``None`` reads the latest computed at the horizon.
        title: replaces the default title.

    Returns:
        ``data`` holds the :func:`readers.survival_diagnostics` result.

    Raises:
        WrongProblemType: the model is not a survival model.
        ValueError: no model has this id.
        NothingToDraw: nothing is persisted, or calibration is undefined (an estimator
            without ``predict_survival_function``, no row followed to the horizon).
    """
    result = _read(pool, "plot survival-calibration", model_id, horizon, as_of_date)
    rows = result["kinds"]["calibration"]
    deciles = [r for r in rows if r["bin"] > 0 and r["value"] is not None]
    population = next((r for r in rows if r["bin"] == 0), None)
    if not deciles:
        why = _undefined(rows) or "no decile is followed to the horizon"
        raise NothingToDraw(
            f"Model {model_id}'s survival calibration is undefined on"
            f" {result['as_of_date']} at horizon {float(result['horizon']):g}: {why}."
            f" Read its ranking instead: triage plot km {model_id}."
        )
    h = float(result["horizon"])
    xs = [r["extra"]["predicted"] for r in deciles]
    ys = [r["value"] for r in deciles]

    with style():
        fig, axes = new_figure(5.6, 4.2)
        ax = axes[0, 0]
        lo, hi = min(xs + ys), max(xs + ys)
        ax.plot([lo, hi], [lo, hi], color=INK, linestyle=BASE_DASH, linewidth=1.0)
        ax.plot(xs, ys, color=AQUA, marker="o", markersize=4)
        for r, x, y in zip(deciles, xs, ys, strict=True):
            ax.annotate(
                str(r["bin"]),
                (x, y),
                xytext=(4, 3),
                textcoords="offset points",
                fontsize=7.5,
            )
        pct_axis(ax, "x")
        pct_axis(ax, "y")
        ax.set_xlabel(f"mean predicted S({h:g})")
        ax.set_ylabel(f"Kaplan-Meier S({h:g})")
        ax.set_title(
            title
            or f"Does model {model_id}'s S({h:g}) match what was observed?"
            f" ({result['as_of_date']})"
        )

    extra = {} if population is None else population["extra"]
    summary = (
        f"{len(deciles)} deciles at {result['as_of_date']}, horizon {h:g}: predicted"
        f" {span(xs)}, observed {span(ys)}"
    )
    if extra.get("ici") is not None:
        summary += f"; ICI {extra['ici']:.3f}, E50 {extra['e50']:.3f}"
    return PlotResult(fig, {"survival": result}, summary)
