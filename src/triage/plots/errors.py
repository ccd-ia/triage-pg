"""The error trees: a model's leaves against the population, a group's leaves by support
and error, and the rules that recur across a group's test dates."""

from __future__ import annotations

import re
from typing import Any, Optional

from matplotlib.patches import Rectangle

from triage.diagnostics import readers
from triage.plots.style import (
    AQUA,
    BASE_DASH,
    BLUE,
    INK,
    ORANGE,
    NothingToDraw,
    PlotResult,
    as_date,
    fmt_rate,
    new_figure,
    no_grid,
    only_cut,
    style,
)
from triage.util.db import DictRowPool

_SIGNATURE_SEP = re.compile(r"(?<=[>=])\|")

#: Read every persisted row; the figure picks what it draws.
_ALL = 1_000_000

_KIND_COLOUR = {"fp": BLUE, "fn": ORANGE, "confusion": AQUA}
_KIND_TITLE = {
    "fp": "fp leaves: among the selected, where are the wrong flags?",
    "fn": "fn leaves: among the passed-over, where are the misses?",
    "confusion": "confusion leaves: where are the mistakes of either kind?",
}
#: What each kind's reference rate is the share of.
_KIND_REFERENCE = {
    "fp": "the whole list",
    "fn": "all passed over",
    "confusion": "all labeled",
}


def _error_tree_command(model: int | str, parameter: str) -> str:
    return f"triage postmodel error-tree {model} -p {parameter} --kind all"


def _rule_label(rule: Optional[str]) -> str:
    return str(rule or "(every row)").replace(" AND ", "\n")


def _signature_label(signature: Optional[str]) -> str:
    """One line per ``feature op`` of a signature. The parts are joined by ``|``, which
    a feature name can hold too (``n_critical|interval=P3M``): a separator is the ``|``
    right after an operator (``>`` or ``<=``)."""
    return "\n".join(_SIGNATURE_SEP.split(signature)) if signature else "(every row)"


def error_rules(
    pool: DictRowPool,
    model_id: int,
    as_of_date: Optional[readers.DateLike] = None,
    parameter: Optional[str] = None,
    kind: Optional[str] = None,
    top: int = 12,
    title: Optional[str] = None,
) -> PlotResult:
    """A model's error-tree leaves with the most errors, each against its population.

    One panel per kind of leaf. A bar is a leaf's error rate, labelled with its errors
    of rows matched; the leaves are those holding the most errors (support × error
    rate). The dashed line is the kind's rate over the whole population at the cut —
    for fp leaves the wrong share of the list (1 − precision@k) — so a leaf reads as
    worse or better than the list as a whole.

    Args:
        pool: the project database.
        model_id: the model.
        as_of_date: the test date; ``None`` reads the latest date with leaves.
        parameter: the cut; ``None`` reads the only cut persisted.
        kind: ``fp``, ``fn`` or ``confusion``; ``None`` draws the fp and fn trees.
        top: the leaves drawn per kind.
        title: replaces the default title.

    Returns:
        ``data`` maps each kind to the :func:`readers.error_rules` rows drawn, the most
        errors first.

    Raises:
        ValueError: ``kind`` is not a leaf kind, or several cuts are persisted and
            ``parameter`` is ``None``.
        NothingToDraw: no leaf is persisted for the model (at the cut and date).
    """
    rows = readers.error_rules(pool, model_id, error_kind=kind, limit=_ALL)
    of_kind = f"{kind} " if kind else ""
    if not rows:
        raise NothingToDraw(
            f"No {of_kind}error trees are persisted for model {model_id} — run:"
            f" {_error_tree_command(model_id, parameter or '<cut>')}"
        )
    if parameter is None:
        parameter = only_cut(
            sorted({r["parameter"] for r in rows}),
            f"Model {model_id}'s error trees",
        )
    at_cut = [r for r in rows if r["parameter"] == parameter]
    if not at_cut:
        raise NothingToDraw(
            f"No {of_kind}error trees are persisted for model {model_id} at"
            f" {parameter} — run: {_error_tree_command(model_id, parameter)}"
        )
    day = (
        max(r["as_of_date"] for r in at_cut)
        if as_of_date is None
        else as_date(as_of_date)
    )
    at_date = [r for r in at_cut if r["as_of_date"] == day]
    if not at_date:
        dates = ", ".join(sorted({str(r["as_of_date"]) for r in at_cut}))
        raise NothingToDraw(
            f"Model {model_id} has no error-tree leaves at {day} and {parameter}; it has"
            f" them at {dates}. Fit them with: {_error_tree_command(model_id, parameter)}"
        )
    present = {r["error_kind"] for r in at_date}
    if kind is not None:
        kinds = [kind]
    else:
        kinds = [k for k in ("fp", "fn") if k in present] or ["confusion"]

    data: dict[str, Any] = {}
    for k in kinds:
        of_k = sorted(
            (r for r in at_date if r["error_kind"] == k),
            key=lambda r: (-int(r["n_errors"]), str(r["rule"])),
        )
        data[k] = of_k[:top]
    # one rate per kind at this date and cut; a leaf fitted on another split has none
    references = {
        k: next(
            (
                r["reference_error_rate"]
                for r in data[k]
                if r["reference_error_rate"] is not None
            ),
            None,
        )
        for k in kinds
    }

    lines = {
        k: sum(_rule_label(r["rule"]).count("\n") + 1 for r in data[k]) for k in kinds
    }
    heights = [0.9 + 0.17 * lines[k] + 0.12 * len(data[k]) for k in kinds]
    with style():
        fig, axes = new_figure(
            8.0,
            sum(heights) + 0.4,
            len(kinds),
            1,
            gridspec_kw={"height_ratios": heights},
        )
        for ax, k in zip(axes[:, 0], kinds, strict=True):
            drawn = data[k][::-1]  # the most errors on top
            ypos = list(range(len(drawn)))
            ax.barh(
                ypos,
                [r["error_rate"] for r in drawn],
                height=0.55,
                color=_KIND_COLOUR[k],
            )
            for y, r in zip(ypos, drawn, strict=True):
                ax.annotate(
                    f"{r['error_rate']:.2f}  ({r['n_errors']:,} of {r['n_matched']:,})",
                    (r["error_rate"], y),
                    xytext=(4, 0),
                    textcoords="offset points",
                    va="center",
                    fontsize=8,
                )
            reference = references[k]
            if reference is not None:
                ax.axvline(reference, color=INK, linewidth=1.0, linestyle=BASE_DASH)
                ax.annotate(
                    f"{_KIND_REFERENCE[k]} {reference:.2f}",
                    (reference, 1.0),
                    xycoords=("data", "axes fraction"),
                    xytext=(3, -2),
                    textcoords="offset points",
                    va="top",
                    fontsize=8,
                )
            ax.set_yticks(ypos, [_rule_label(r["rule"]) for r in drawn], fontsize=7.5)
            ax.set_xlim(0, 1.15)
            ax.set_ylim(-0.6, max(len(drawn), 1) - 0.4)
            ax.set_xlabel("error rate in the leaf (label: errors of rows matched)")
            ax.set_title(_KIND_TITLE[k])
            no_grid(ax, "y")
        fig.suptitle(
            title or f"Error rules of model {model_id} at {day}, cut {parameter}",
            x=0.01,
            ha="left",
            fontsize=10.5,
            fontweight="bold",
            color=INK,
        )

    parts = [
        f"{len(data[k])} {k} leaves against {fmt_rate(references[k])}" for k in kinds
    ]
    summary = f"model {model_id} at {day}, {parameter}: " + ", ".join(parts)
    return PlotResult(fig, data, summary)


def leaves(
    pool: DictRowPool,
    model_group_id: int,
    parameter: str,
    max_error: float = 0.02,
    min_support: int = 30,
    title: Optional[str] = None,
) -> PlotResult:
    """Every fp and confusion leaf of a model group at one cut, by the share of the leaf
    the model gets wrong and the leaf's size: are any suspiciously right?

    The shaded zone holds the leaves with a wrong share of at most ``max_error`` and at
    least ``min_support`` rows — what ``triage postmodel error-rules --suspicious``
    lists. A leaf there is a segment the model almost never gets wrong, which is worth
    checking for a feature that knows the outcome.

    Args:
        pool: the project database.
        model_group_id: the model group.
        parameter: the cut the trees were fitted at.
        max_error: the zone's largest wrong share.
        min_support: the zone's fewest rows.
        title: replaces the default title.

    Returns:
        ``data`` maps ``fp`` and ``confusion`` to their :func:`readers.group_leaves`
        rows.

    Raises:
        NotFoundError: the model group is unknown.
        NothingToDraw: no fp or confusion leaf is persisted for the group at the cut.
    """
    rows = readers.group_leaves(pool, model_group_id, parameter, ("fp", "confusion"))
    if not rows:
        raise NothingToDraw(
            f"No fp or confusion leaves are persisted for model group {model_group_id}"
            f" at {parameter} — run, for each of its models:"
            f" {_error_tree_command('<model_id>', parameter)}"
        )
    data: dict[str, Any] = {
        k: [r for r in rows if r["error_kind"] == k] for k in ("fp", "confusion")
    }
    drawn = [r for r in rows if r["wrong_share"] is not None]
    in_zone = [
        r
        for r in drawn
        if r["wrong_share"] <= max_error and r["n_matched"] >= min_support
    ]
    sizes = [int(r["n_matched"]) for r in drawn] or [min_support]
    lo = min(min(sizes), min_support) / 1.6
    hi = max(max(sizes), min_support) * 1.8
    with style():
        fig, axes = new_figure(7.2, 3.8)
        ax = axes[0, 0]
        ax.add_patch(
            Rectangle(
                (0, min_support),
                max_error,
                hi - min_support,
                color=ORANGE,
                alpha=0.18,
                linewidth=0,
            )
        )
        ax.annotate(
            f"suspicious zone\nwrong ≤ {max_error:g}, n ≥ {min_support:,}",
            (max_error, hi),
            xytext=(5, -4),
            textcoords="offset points",
            va="top",
            fontsize=8,
        )
        for k, colour, label in (
            ("fp", BLUE, "fp leaves (error rate)"),
            ("confusion", AQUA, "confusion leaves (1 − TP share)"),
        ):
            sel = [r for r in data[k] if r["wrong_share"] is not None]
            ax.scatter(
                [r["wrong_share"] for r in sel],
                [r["n_matched"] for r in sel],
                s=18,
                color=colour,
                edgecolors="none",
                alpha=0.85,
                label=f"{label}: {len(sel)}",
            )
        ax.set_yscale("log")
        ax.set_ylim(lo, hi)
        ax.set_xlim(0, 1)
        ax.set_xlabel("share of the leaf the model gets wrong")
        ax.set_ylabel("rows in the leaf (log)")
        ax.set_title(
            title
            or f"Model group {model_group_id} at {parameter}: {len(in_zone)} of"
            f" {len(drawn)} leaves in the suspicious zone"
        )
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
    summary = f"{len(drawn)} leaves, {len(in_zone)} in the suspicious zone"
    return PlotResult(fig, data, summary)


def recurrence(
    pool: DictRowPool,
    model_group_id: int,
    parameter: str,
    kind: str = "fp",
    top: int = 12,
    title: Optional[str] = None,
) -> PlotResult:
    """Which error rules recur across a model group's test dates?

    Rules are matched by signature: their features and split directions, without
    thresholds. Each row is a signature, each column a test date the group's trees were
    fitted on; a dot marks a date with a leaf of that signature, labelled with the
    leaf's error rate (the largest, when several leaves share it). A signature found on
    most dates is a stable failure mode, and a candidate for a missing feature.

    Args:
        pool: the project database.
        model_group_id: the model group.
        parameter: the cut the trees were fitted at.
        kind: ``fp``, ``fn`` or ``confusion``.
        top: the signatures drawn, the most recurrent first.
        title: replaces the default title.

    Returns:
        ``data`` holds ``rules`` (the :func:`readers.error_recurrence` rules drawn) and
        ``leaves`` (the :func:`readers.group_leaves` rows of those signatures).

    Raises:
        NotFoundError: the model group is unknown.
        ValueError: ``kind`` is not a leaf kind.
        NothingToDraw: no leaf of the kind is persisted for the group at the cut.
    """
    found = readers.error_recurrence(pool, model_group_id, parameter, kind, top)
    rules = found["rules"]
    if parameter not in found["parameters"] or not rules:
        cuts = ", ".join(found["parameters"]) or "none"
        raise NothingToDraw(
            f"No {kind} error rules with a signature are persisted for model group"
            f" {model_group_id} at {parameter} (its cuts: {cuts}) — run, for each of"
            f" its models: {_error_tree_command('<model_id>', parameter)}"
        )
    signatures = [r["signature"] for r in rules]
    group_rows = readers.group_leaves(pool, model_group_id, parameter, (kind,))
    dates = sorted({r["as_of_date"] for r in group_rows})
    matched = [r for r in group_rows if r["signature"] in signatures]
    data: dict[str, Any] = {"rules": rules, "leaves": matched}

    cells: dict[tuple[Any, Any], float] = {}
    for r in matched:
        key = (r["signature"], r["as_of_date"])
        cells[key] = max(cells.get(key, 0.0), float(r["error_rate"]))
    labels = [_signature_label(s) for s in signatures]
    n_lines = sum(label.count("\n") + 1 for label in labels)
    with style():
        fig, axes = new_figure(
            3.2 + 0.85 * len(dates) + 2.6, 1.3 + 0.2 * n_lines + 0.15 * len(rules)
        )
        ax = axes[0, 0]
        ypos = list(range(len(rules)))[::-1]  # the most recurrent on top
        for y, sig in zip(ypos, signatures, strict=True):
            ax.axhline(y, color=INK, linewidth=0.5, alpha=0.3)
            for x, d in enumerate(dates):
                rate = cells.get((sig, d))
                if rate is None:
                    continue
                ax.scatter([x], [y], s=60, color=_KIND_COLOUR[kind], zorder=3)
                ax.annotate(
                    f"{rate:.2f}",
                    (x, y),
                    xytext=(0, 7),
                    textcoords="offset points",
                    ha="center",
                    fontsize=7,
                )
        for y, r in zip(ypos, rules, strict=True):
            ax.annotate(
                f"{r['n_dates']} of {r['n_dates_total']} dates · mean error"
                f" {fmt_rate(r['mean_error_rate'])} · mean n {r['mean_support']:,.0f}",
                (1.0, y),
                xycoords=("axes fraction", "data"),
                xytext=(8, 0),
                textcoords="offset points",
                va="center",
                fontsize=7.5,
            )
        ax.set_yticks(ypos, labels, fontsize=7.5)
        ax.set_xticks(range(len(dates)), [str(d) for d in dates], fontsize=7.5)
        ax.set_xlim(-0.6, max(len(dates), 1) - 0.4)
        ax.set_ylim(-0.7, len(rules) - 0.3)
        ax.set_xlabel(
            "test date (dot: a leaf with this signature; label: its error rate)"
        )
        ax.grid(visible=False)
        ax.set_title(
            title
            or f"{kind} rules that recur — model group {model_group_id} at {parameter}"
        )
    top_rule = rules[0]
    summary = (
        f"{len(rules)} {kind} signature(s) over {len(dates)} test date(s); the most"
        f" recurrent on {top_rule['n_dates']} of {top_rule['n_dates_total']}"
    )
    return PlotResult(fig, data, summary)
