"""The threshold curve (the Rayid curve): one model's, and a model group's across dates."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, Optional

from triage.diagnostics import readers
from triage.plots.style import (
    BASE_DASH,
    INK,
    SERIES,
    NothingToDraw,
    PlotResult,
    check_series,
    fmt_rate,
    nan,
    new_figure,
    pct_axis,
    span,
    style,
)
from triage.util.db import DictRowPool


def _population_label(name: Optional[str], subset_hash: str) -> str:
    return name or subset_hash[:12]


#: Precision is drawn from this share of the population on: the first cuts hold a
#: handful of entities, and their 0 or 1 reads as a feature of the ranking.
MIN_SHARE = 0.005


def _draw_precision(
    ax: Any, rows: Sequence[dict[str, Any]], colour: str, label: Optional[str] = None
) -> None:
    shown = [r for r in rows if r["pct"] >= MIN_SHARE]
    ax.plot(
        [r["pct"] for r in shown],
        [nan(r["prec"]) for r in shown],
        color=colour,
        label=label,
    )


def curve(
    pool: DictRowPool,
    model_id: int,
    as_of_date: Optional[readers.DateLike] = None,
    subsets: Sequence[str] = (),
    compare: Optional[int] = None,
    points: Optional[int] = 101,
    title: Optional[str] = None,
) -> PlotResult:
    """Precision and recall against the share of the population selected, per series.

    A series is a model over a population: everyone, and each Subset given (a Subset is
    its own population, ranked among its members). With ``compare``, the second model —
    a baseline floor, another model group's model — is drawn over the same populations
    at the same test date. Each series carries its own base rate, the dashed line in its
    colour on the precision panel. Precision is drawn from :data:`MIN_SHARE` of the
    population on; ``data`` keeps every row read.

    Args:
        pool: the project database.
        model_id: the model.
        as_of_date: the test date; ``None`` reads each model's latest test date.
        subsets: Subset names or subset_hashes (a prefix is enough), drawn next to
            everyone.
        compare: a second model drawn at the same test date.
        points: thinning of each curve (:func:`readers.curve`); ``None`` draws every k.
        title: replaces the default title.

    Returns:
        ``data`` maps each series label to its :func:`readers.curve` rows.

    Raises:
        NotFoundError: a Subset matches nothing.
        ValueError: an ambiguous Subset prefix, more than eight series, or ``points``
            below 2.
        NothingToDraw: a model has no test predictions at the date.
    """
    if compare == model_id:
        raise ValueError(
            f"compare={compare} is model {model_id} itself — compare it with another model"
        )
    populations: list[tuple[str, str]] = [("everyone", "")]
    for value in subsets:
        subset_hash, name = readers.resolve_subset(pool, value)
        if any(h == subset_hash for _, h in populations):
            raise ValueError(f"Subset {value!r} is given twice — pass each Subset once")
        label = _population_label(name, subset_hash)
        if any(lbl == label for lbl, _ in populations):
            label = f"{label} ({subset_hash[:8]})"  # two Subsets share a name
        populations.append((label, subset_hash))
    models = [model_id] if compare is None else [model_id, compare]
    check_series(
        len(models) * len(populations),
        f"curves ({len(models)} model(s) × {len(populations)} population(s))",
        "pass fewer --subset values, or draw the compared model in its own figure",
    )

    data: dict[str, Any] = {}
    for m in models:
        for label, subset_hash in populations:
            rows = readers.curve(pool, m, as_of_date, subset_hash, points)
            if not rows:
                where = f" in Subset {label}" if subset_hash else ""
                at = f" at {as_of_date}" if as_of_date is not None else ""
                raise NothingToDraw(
                    f"Model {m} has no test predictions{where}{at} — the curve is drawn"
                    " over one scored test date. Score the model first: triage run"
                    " <config> (or triage score for a new date)."
                )
            name = label if compare is None else f"model {m}"
            if compare is not None and len(populations) > 1:
                name = f"model {m} · {label}"
            data[name] = rows

    if as_of_date is not None:
        when = f"at {as_of_date}"
    else:
        when = (
            "at each model's latest test date" if compare else "at its latest test date"
        )
    with style():
        fig, axes = new_figure(10.0, 3.6, 1, 2, sharex=True)
        prec_ax, rec_ax = axes[0]
        for (name, rows), colour in zip(data.items(), SERIES, strict=False):
            _draw_precision(prec_ax, rows, colour)
            rec_ax.plot(
                [r["pct"] for r in rows],
                [nan(r["rec"]) for r in rows],
                color=colour,
                label=name,
            )
            base = rows[-1]["base_rate"]
            if base is not None:
                prec_ax.axhline(base, color=colour, linewidth=1.0, linestyle=BASE_DASH)
        rec_ax.plot([0, 1], [0, 1], color=INK, linewidth=0.8, linestyle=BASE_DASH)
        for ax, label in ((prec_ax, "precision"), (rec_ax, "recall")):
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            pct_axis(ax)
            ax.set_xlabel("population selected (ranked by score)")
            ax.set_ylabel(label)
        prec_ax.set_title("Precision (dashed: each series' base rate)")
        rec_ax.set_title("Recall (dashed: a random ranking)")
        rec_ax.legend(loc="lower right")
        models_text = " and ".join(f"model {m}" for m in models)
        fig.suptitle(
            title or f"Threshold curve of {models_text} {when}",
            x=0.01,
            ha="left",
            fontsize=10.5,
            fontweight="bold",
            color=INK,
        )

    head = f"{models_text} {when}"
    parts = [
        f"{name}: {rows[-1]['k']:,} scored, base rate {fmt_rate(rows[-1]['base_rate'], 3)}"
        for name, rows in data.items()
    ]
    return PlotResult(fig, data, f"{head} — " + "; ".join(parts))


def curve_dates(
    pool: DictRowPool,
    model_group_id: int,
    subset: str = "",
    points: Optional[int] = 101,
    title: Optional[str] = None,
) -> PlotResult:
    """A model group's precision curve on each of its test dates, with each date's base
    rate: is the ranking stable over time?

    Each test date is scored by one of the group's models; adjacent dates of one model
    share the model, so a jump between them is a change in the data.

    Args:
        pool: the project database.
        model_group_id: the model group.
        subset: a Subset name or subset_hash (a prefix is enough); ``''`` is everyone.
        points: thinning of each curve (:func:`readers.curve`); ``None`` draws every k.
        title: replaces the default title.

    Returns:
        ``data`` maps ``'<date> · model <id>'`` to the :func:`readers.group_curves`
        series (``model_id``, ``as_of_date``, ``base_rate``, ``population``,
        ``points``).

    Raises:
        NotFoundError: the model group, or the Subset, is unknown.
        ValueError: more than eight test dates, an ambiguous Subset prefix, or
            ``points`` below 2.
        NothingToDraw: the group has no test predictions (in the Subset).
    """
    subset_hash, subset_name = ("", None)
    if subset:
        subset_hash, subset_name = readers.resolve_subset(pool, subset)
    series = readers.group_curves(pool, model_group_id, subset_hash, points)
    where = (
        f" in Subset {_population_label(subset_name, subset_hash)}"
        if subset_hash
        else ""
    )
    if not series:
        raise NothingToDraw(
            f"Model group {model_group_id} has no test predictions{where} — score its"
            " models first: triage run <config>."
        )
    check_series(
        len(series),
        f"test-date curves of model group {model_group_id}",
        "draw one date at a time with triage plot curve MODEL --date D",
    )
    data = {f"{s['as_of_date']} · model {s['model_id']}": s for s in series}

    with style():
        fig, axes = new_figure(10.0, 3.6, 1, 2, gridspec_kw={"width_ratios": [1.5, 1]})
        ax, base_ax = axes[0]
        colours = SERIES[: len(series)]
        for (name, s), colour in zip(data.items(), colours, strict=True):
            _draw_precision(ax, s["points"], colour, name)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        pct_axis(ax)
        ax.set_xlabel("population selected (ranked by score)")
        ax.set_ylabel("precision")
        ax.set_title("One curve per test date")
        ax.legend(loc="upper right", fontsize=7.5)

        xs = list(range(len(series)))
        rates = [nan(s["base_rate"]) for s in series]
        base_ax.plot(xs, rates, color=INK, linewidth=1.0, linestyle=BASE_DASH)
        base_ax.scatter(xs, rates, s=36, c=list(colours), zorder=3)
        for x, s in zip(xs, series, strict=True):
            base_ax.annotate(
                fmt_rate(s["base_rate"]),
                (x, nan(s["base_rate"])),
                xytext=(0, 6),
                textcoords="offset points",
                ha="center",
                fontsize=7.5,
            )
        # the scored rows go under each date: above the dot, six labels collide
        base_ax.set_xticks(
            xs,
            [f"{_short_date(s['as_of_date'])}\n{s['population']:,}" for s in series],
            fontsize=7.5,
        )
        base_ax.set_xlim(-0.6, len(series) - 0.4)
        base_ax.set_ylim(0, 1)
        base_ax.set_xlabel("test date, and the rows scored on it")
        base_ax.set_ylabel("base rate (labeled positives ÷ labeled)")
        base_ax.set_title("The base rate of each date")
        fig.suptitle(
            title or f"Model group {model_group_id} across its test dates{where}",
            x=0.01,
            ha="left",
            fontsize=10.5,
            fontweight="bold",
            color=INK,
        )

    first, last = series[0]["as_of_date"], series[-1]["as_of_date"]
    summary = (
        f"{len(series)} curve(s) of model group {model_group_id}{where},"
        f" {first} to {last}: base rate {span([s['base_rate'] for s in series])}"
    )
    return PlotResult(fig, data, summary)


def _short_date(value: date | str) -> str:
    """``2016-10-01`` → ``2016-10``: a test-date tick."""
    return str(value)[:7]
