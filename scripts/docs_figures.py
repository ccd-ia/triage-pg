"""Regenerate every figure of the docs site's reading guides (plan phase 7).

    uv run python scripts/docs_figures.py                  # both sets
    uv run python scripts/docs_figures.py --illustrative   # seeded, no database
    uv run python scripts/docs_figures.py --worked         # the DirtyDuck tutorial DB
    just docs-figures

Illustrative figures come from a fixed seed and need no database: the curve
archetypes, a subgroup ranked as a block, the leakage signature on a curve, the
confusion tree's four classes on a toy two-feature dataset, the selected-versus-rest
crosstab bars, and a good and a bad calibration plot.

Worked figures read the DirtyDuck tutorial database (``just tutorial-up``; PG* or
DATABASE_URL in the environment) after a run of
``example/dirtyduck/experiment-guides.yaml`` and its diagnostics. Fourteen of them
(the curves, error rules, leaves, rule recurrence, crosstabs and list overlap) are
drawn by :mod:`triage.plots`, exactly as ``triage plot`` draws them, with a provenance
line added under each; the guides print the command beside each figure. The other
five (calibration, score histogram, Subset metrics, feature ablation, bias) read the
dashboard's routes (``triage.dashboard.routes``) and are drawn here. The experiment
and every model are resolved by query (the audition pick at the experiment's primary
cut); no id is written in this file, except the crosstabs features and Subsets the
guides name. The script never runs an experiment; when something is missing it prints
the command to run.

The ``worked-chicago311-*`` and ``worked-donorschoose-*`` figures are not drawn here:
each is the output of the ``triage plot`` command printed under it in the guide, run
against that tutorial's database.

Output: ``docs-site/src/assets/guides/*.svg``, on a transparent background, in mid-tone
colours that read on the site's light and dark themes. Text is laid out with the
DejaVu Sans metrics bundled with matplotlib and rendered with the reader's system
sans, so the files are byte-identical across runs and machines (fixed
``svg.hashsalt``, no date).
"""

from __future__ import annotations

import json
import math
import os
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import typer  # noqa: E402
import yaml  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter, PercentFormatter  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "docs-site" / "src" / "assets" / "guides"
GUIDES_CONFIG = REPO / "example" / "dirtyduck" / "experiment-guides.yaml"
SEED = 20261028

# Mid-tone steps that hold >= 3:1 on both a white page and the dark theme's near-black
# (dataviz validator: lightness band, chroma floor and adjacent CVD separation pass on
# both surfaces). INK is the one text colour; it reads at about 4:1 on both.
INK = "#807e78"
BLUE = "#3987e5"
ORANGE = "#d95926"
AQUA = "#199e70"
YELLOW = "#c98500"
SERIES = (BLUE, ORANGE, AQUA, YELLOW)
# One-hue ordinal ramp for ordered series (test dates), light to dark.
DATE_RAMP = ("#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf")
SYSTEM_SANS = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
BASE_DASH = (0, (4, 3))  # the base-rate reference line, never a gridline

app = typer.Typer(add_completion=False, help=__doc__)


# ---------------------------------------------------------------- style and output
def _style() -> None:
    plt.rcParams.update(
        {
            "svg.hashsalt": "triage-pg-guides",
            "svg.fonttype": "none",
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans"],
            "font.size": 9.5,
            "text.color": INK,
            "axes.labelcolor": INK,
            "axes.edgecolor": INK,
            "axes.linewidth": 0.6,
            "axes.titlesize": 10.5,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlecolor": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.axisbelow": True,
            "grid.color": INK,
            "grid.alpha": 0.22,
            "grid.linewidth": 0.6,
            "xtick.color": INK,
            "ytick.color": INK,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "lines.linewidth": 1.6,
            "lines.solid_capstyle": "round",
            "legend.frameon": False,
            "legend.fontsize": 8.5,
            "figure.dpi": 100,
            "savefig.transparent": True,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.08,
        }
    )


@dataclass
class Written:
    """Every file one invocation wrote, and the numbers behind the worked figures."""

    files: list[Path] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


def _save(fig: Figure, name: str, out_dir: Path, written: Written, note: str) -> None:
    """Write ``fig`` as ``name`` with ``note``, the provenance line wrapped under it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    fig.tight_layout()
    width_chars = int(fig.get_figwidth() * 15.5)
    fig.text(
        0.005,
        0.0,
        textwrap.fill(note, width_chars),
        ha="left",
        va="top",
        fontsize=8,
        color=INK,
    )
    fig.savefig(path, format="svg", metadata={"Date": None, "Creator": None})
    plt.close(fig)
    text = path.read_text(encoding="utf-8")
    text = text.replace("'DejaVu Sans', sans-serif", SYSTEM_SANS)
    path.write_text(text, encoding="utf-8")
    written.files.append(path)
    typer.echo(f"  wrote {path.relative_to(REPO)}")


def _log_ratio_axis(ax: Axes) -> None:
    """A log10 ratio axis: the 1× line and ticks read back as multiples."""
    ax.axvline(0, color=INK, linewidth=0.8)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{10**v:.2g}×"))


def _pct_axis(ax: Axes, axis: str = "x") -> None:
    fmt = PercentFormatter(1.0, decimals=0)
    (ax.xaxis if axis == "x" else ax.yaxis).set_major_formatter(fmt)


def _base_rate(
    ax: Axes, rate: float, label: str = "base rate", *, legend: bool = False
) -> None:
    """Dashed line at ``rate``, labelled in the legend or annotated at the left edge."""
    text = f"{label} {rate:.2f}"
    if legend:
        ax.axhline(rate, color=INK, linewidth=1.0, linestyle=BASE_DASH, label=text)
        return
    ax.axhline(rate, color=INK, linewidth=1.0, linestyle=BASE_DASH)
    ax.annotate(
        text,
        xy=(0.0, rate),
        xycoords=("axes fraction", "data"),
        xytext=(3, -3),
        textcoords="offset points",
        ha="left",
        va="top",
        fontsize=8,
        color=INK,
    )


def _curve(score: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, ...]:
    """Population share, precision and recall at 400 cuts of a ranking by score."""
    order = np.argsort(-score, kind="stable")
    hits = np.cumsum(y[order])
    k = np.arange(1, len(y) + 1)
    idx = np.unique(np.linspace(0, len(y) - 1, min(len(y), 400)).round().astype(int))
    pct, prec, rec = k / len(y), hits / k, hits / max(int(hits[-1]), 1)
    return pct[idx], prec[idx], rec[idx]


# ---------------------------------------------------------------- illustrative set
def fig_curve_archetypes(rng: np.random.Generator, out: Path, w: Written) -> None:
    n, base = 5000, 0.25
    y = (rng.random(n) < base).astype(float)
    ranks = y * 1.4 + rng.normal(0.0, 1.0, n)
    random_score = rng.random(n)
    # A block: one group (30%) at a 0.45 base rate, scored above everyone else, with no
    # signal inside it (and none among the rest).
    block = rng.random(n) < 0.30
    y_block = (rng.random(n) < np.where(block, 0.45, 0.165)).astype(float)
    block_score = block * 1.0 + rng.random(n) * 0.5

    fig, axes = plt.subplots(1, 4, figsize=(10.0, 2.7), sharey=True)
    panels = (
        ("Ranks well", ranks, y, "prec"),
        ("Flat at the base rate", random_score, y, "prec"),
        ("Flat above the base rate", block_score, y_block, "prec"),
        ("Recall on the diagonal", random_score, y, "rec"),
    )
    for ax, (title, score, labels, kind) in zip(axes, panels, strict=True):
        pct, prec, rec = _curve(score, labels)
        keep = pct >= 0.01  # the first cuts are a handful of rows
        if kind == "prec":
            ax.plot(pct[keep], prec[keep], color=BLUE)
            _base_rate(ax, float(labels.mean()))
        else:
            _, _, rec_good = _curve(ranks, y)
            ax.plot([0, 1], [0, 1], color=INK, linewidth=0.8, linestyle=BASE_DASH)
            ax.plot(pct, rec, color=BLUE, label="random ranking")
            ax.plot(pct, rec_good, color=ORANGE, label="a model that ranks")
            ax.legend(loc="lower right")
        ax.set_title(title)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        _pct_axis(ax)
        ax.set_xlabel("population selected")
    axes[0].set_ylabel("precision (recall in the last panel)")
    _save(
        fig,
        "curve-archetypes.svg",
        out,
        w,
        "Illustrative: 5,000 simulated entities, base rate 0.25 (seed"
        f" {SEED}). Third panel: a group of 30% at base rate 0.45 is scored above"
        " everyone, with no ranking inside it.",
    )


def fig_curve_subgroup_block(rng: np.random.Generator, out: Path, w: Written) -> None:
    n = 6000
    in_b = rng.random(n) < 0.4
    y = (rng.random(n) < np.where(in_b, 0.40, 0.20)).astype(float)
    # Group A is ranked from within (its score carries the label); group B is lifted
    # as a block, and inside it the score is noise.
    score = np.where(
        in_b, 0.6 + rng.normal(0.0, 0.25, n), y * 1.3 + rng.normal(0, 1, n)
    )
    fig, axes = plt.subplots(1, 3, figsize=(10.0, 2.8), sharey=True)
    for ax, (title, mask) in zip(
        axes,
        (
            ("Everyone", np.ones(n, dtype=bool)),
            ("Subgroup A: ranked", ~in_b),
            ("Subgroup B: a block", in_b),
        ),
        strict=True,
    ):
        pct, prec, _ = _curve(score[mask], y[mask])
        keep = pct >= 0.01
        ax.plot(pct[keep], prec[keep], color=BLUE)
        _base_rate(ax, float(y[mask].mean()))
        if not mask.all():
            ax.axhline(float(y.mean()), color=INK, linewidth=0.8, alpha=0.6)
            ax.annotate(
                f"everyone {y.mean():.2f}",
                xy=(1.0, float(y.mean())),
                xycoords=("axes fraction", "data"),
                xytext=(0, 3),
                textcoords="offset points",
                ha="right",
                va="bottom",
                fontsize=8,
            )
        ax.set_title(title)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        _pct_axis(ax)
        ax.set_xlabel("share of the population drawn")
    axes[0].set_ylabel("precision")
    _save(
        fig,
        "curve-subgroup-block.svg",
        out,
        w,
        "Illustrative (seed"
        f" {SEED}). Each subgroup is re-ranked inside itself, as `triage model curve"
        " --subset` does. B sits above everyone's base rate but is flat at its own.",
    )


def fig_curve_leakage(rng: np.random.Generator, out: Path, w: Written) -> None:
    n, base = 5000, 0.25
    y = (rng.random(n) < base).astype(float)
    honest = y * 1.0 + rng.normal(0.0, 1.0, n)
    # A feature that knows the outcome for 40% of the positives puts them on top.
    leaked = honest + np.where((y == 1) & (rng.random(n) < 0.4), 10.0, 0.0)
    fig, ax = plt.subplots(figsize=(6.0, 3.0))
    for score, colour, label in (
        (leaked, ORANGE, "with a feature that knows the outcome"),
        (honest, BLUE, "without it"),
    ):
        pct, prec, _ = _curve(score, y)
        keep = pct >= 0.005
        ax.plot(pct[keep], prec[keep], color=colour, label=label)
    _base_rate(ax, float(y.mean()))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.04)
    _pct_axis(ax)
    ax.set_xlabel("population selected")
    ax.set_ylabel("precision")
    ax.set_title("A leak reads as precision near 1 at the top")
    ax.legend(loc="center right")
    _save(
        fig,
        "curve-leakage-signature.svg",
        out,
        w,
        f"Illustrative (seed {SEED}), not DirtyDuck.",
    )


def _tree_rectangles(
    tree: Any, bounds: tuple[float, float, float, float]
) -> list[tuple[tuple[float, float, float, float], int]]:
    """Leaf rectangles (x0, x1, y0, y1) of a fitted two-feature tree, with leaf ids."""
    t = tree.tree_
    out: list[tuple[tuple[float, float, float, float], int]] = []

    def walk(node: int, box: tuple[float, float, float, float]) -> None:
        if t.children_left[node] == -1:
            out.append((box, node))
            return
        x0, x1, y0, y1 = box
        f, thr = int(t.feature[node]), float(t.threshold[node])
        if f == 0:
            walk(int(t.children_left[node]), (x0, thr, y0, y1))
            walk(int(t.children_right[node]), (thr, x1, y0, y1))
        else:
            walk(int(t.children_left[node]), (x0, x1, y0, thr))
            walk(int(t.children_right[node]), (x0, x1, thr, y1))

    walk(0, bounds)
    return out


def fig_confusion_tree_toy(rng: np.random.Generator, out: Path, w: Written) -> None:
    from sklearn.tree import DecisionTreeClassifier

    n = 600
    x1, x2 = rng.random(n), rng.random(n)
    # The outcome depends on both features, and on a pocket the model never learned
    # (x2 > 0.8, x1 < 0.35): that pocket is where the passed-over positives gather.
    logit = 3.2 * x1 + 1.2 * x2 - 3.0 + np.where((x2 > 0.8) & (x1 < 0.35), 2.6, 0.0)
    y = rng.random(n) < 1.0 / (1.0 + np.exp(-logit))
    score = 3.2 * x1 + 1.2 * x2
    k = int(0.25 * n)
    cut = float(np.sort(score)[::-1][k - 1])
    selected = score >= cut
    classes = np.where(selected, np.where(y, "tp", "fp"), np.where(y, "fn", "tn"))
    tree = DecisionTreeClassifier(max_depth=2, min_samples_leaf=30, random_state=0)
    tree.fit(np.column_stack([x1, x2]), classes)

    fig, ax = plt.subplots(figsize=(7.6, 5.0))
    leaf_lines: list[str] = []
    leaves = sorted(
        _tree_rectangles(tree, (0.0, 1.0, 0.0, 1.0)), key=lambda b: (b[0][0], -b[0][3])
    )
    for letter, ((bx0, bx1, by0, by1), _node) in zip("ABCDEFGH", leaves, strict=False):
        ax.add_patch(
            Rectangle(
                (bx0, by0),
                bx1 - bx0,
                by1 - by0,
                fill=False,
                edgecolor=INK,
                linewidth=1.0,
                linestyle=BASE_DASH,
            )
        )
        in_leaf = (x1 > bx0) & (x1 <= bx1) & (x2 > by0) & (x2 <= by1)
        counts = {
            c: int((classes[in_leaf] == c).sum()) for c in ("tp", "fp", "fn", "tn")
        }
        leaf_lines.append(
            f"{letter}: " + "  ".join(f"{c.upper()} {v}" for c, v in counts.items())
        )
        ax.annotate(
            letter,
            xy=((bx0 + bx1) / 2, (by0 + by1) / 2),
            ha="center",
            va="center",
            fontsize=11,
            fontweight="bold",
            bbox={"boxstyle": "circle,pad=0.25", "fc": "none", "ec": INK, "lw": 1.0},
        )
    styles = {
        "tp": (ORANGE, ORANGE, "TP: selected, failed"),
        "fp": (BLUE, BLUE, "FP: selected, passed"),
        "fn": ("none", ORANGE, "FN: passed over, failed"),
        "tn": ("none", BLUE, "TN: passed over, passed"),
    }
    for cls, (face, edge, label) in styles.items():
        m = classes == cls
        ax.scatter(
            x1[m],
            x2[m],
            s=16,
            facecolors=face,
            edgecolors=edge,
            linewidths=1.0,
            label=label,
        )
    xs = np.linspace(0, 1, 50)
    ax.plot(xs, (cut - 3.2 * xs) / 1.2, color=INK, linewidth=1.4)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("feature 1 (the model weighs it heavily)")
    ax.set_ylabel("feature 2")
    ax.set_title("The confusion tree's four classes, and its leaves")
    handles, labels = ax.get_legend_handles_labels()
    handles += [
        Line2D([0], [0], color=INK, linewidth=1.4),
        Line2D([0], [0], color=INK, linewidth=1.0, linestyle=BASE_DASH),
    ]
    labels += ["selection boundary (top 25%)", "confusion-tree leaf"]
    ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    ax.text(
        1.03,
        0.42,
        "Leaves (rows per class)\n" + "\n".join(leaf_lines),
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=8.5,
        linespacing=1.5,
    )
    ax.grid(False)
    _save(
        fig,
        "confusion-tree-toy.svg",
        out,
        w,
        f"Illustrative: 600 simulated entities (seed {SEED}), a depth-2 tree with"
        " at least 30 rows per leaf. Its first split redraws the selection boundary;"
        " the passed-over failures gather top left, where the model's score is low.",
    )


def fig_crosstab_bars(out: Path, w: Written) -> None:
    rows = (
        ("critical violation in the last 6 months", 0.62, 0.18),
        ("restaurant", 0.70, 0.60),
        ("zip code 60622", 0.06, 0.03),
        ("no inspection on record (missing flag)", 0.30, 0.05),
        ("re-inspection already scheduled", 0.97, 0.02),
    )
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    ypos = np.arange(len(rows))[::-1]
    h = 0.34
    ax.barh(
        ypos + h / 2, [r[1] for r in rows], height=h, color=BLUE, label="on the list"
    )
    ax.barh(
        ypos - h / 2, [r[2] for r in rows], height=h, color=ORANGE, label="the rest"
    )
    for yv, (_, on, off) in zip(ypos, rows, strict=True):
        for value, bar_y in ((on, yv + h / 2), (off, yv - h / 2)):
            ax.annotate(
                f"{value:.2f}",
                (value, bar_y),
                xytext=(3, 0),
                textcoords="offset points",
                va="center",
                fontsize=8,
            )
    ax.set_yticks(ypos, [r[0] for r in rows])
    ax.set_xlim(0, 1.08)
    _pct_axis(ax)
    ax.set_xlabel("share of entities with a nonzero value")
    ax.set_title("Selected versus the rest: nonzero rates")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
    ax.grid(axis="y", visible=False)
    _save(
        fig,
        "crosstab-selected-vs-rest.svg",
        out,
        w,
        "Illustrative values, not DirtyDuck. The last row is the leakage signature"
        " (near 1 on the list, near 0 off it); the missing flag concentrated on the list"
        " is a data-quality signal; the zip code is a proxy to check for fairness.",
    )


def _deciles(score: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(-score, kind="stable")
    groups = np.array_split(order, 10)
    return (
        np.array([score[g].mean() for g in groups]),
        np.array([y[g].mean() for g in groups]),
    )


def fig_calibration(rng: np.random.Generator, out: Path, w: Written) -> None:
    n = 8000
    p = rng.beta(2.0, 5.0, n)
    y_good = (rng.random(n) < p).astype(float)
    # Overconfident: the true rate moves only a third as much as the score does.
    y_bad = (rng.random(n) < 0.20 + 0.33 * (p - 0.2)).astype(float)
    fig, axes = plt.subplots(1, 2, figsize=(7.6, 3.4), sharey=True)
    for ax, (title, y) in zip(
        axes, (("Calibrated", y_good), ("Ranks, but overconfident", y_bad)), strict=True
    ):
        s, r = _deciles(p, y)
        ax.plot([0, 1], [0, 1], color=INK, linewidth=0.8, linestyle=BASE_DASH)
        ax.plot(s, r, color=BLUE, marker="o", markersize=5, markeredgecolor="none")
        ax.set_xlim(0, 0.8)
        ax.set_ylim(0, 0.8)
        ax.set_title(title)
        ax.set_xlabel("mean score in the decile")
    axes[0].set_ylabel("observed outcome rate")
    _save(
        fig,
        "calibration-good-bad.svg",
        out,
        w,
        f"Illustrative (seed {SEED}): ten score deciles, as the model card's"
        " calibration panel draws them. Both models rank the same way; only the left"
        " one's scores can be read as probabilities.",
    )


def illustrative(out: Path, w: Written) -> None:
    typer.echo("Illustrative figures (seeded, no database):")
    rng = np.random.default_rng(SEED)
    fig_curve_archetypes(rng, out, w)
    fig_curve_subgroup_block(np.random.default_rng(SEED + 1), out, w)
    fig_curve_leakage(np.random.default_rng(SEED + 2), out, w)
    fig_confusion_tree_toy(np.random.default_rng(SEED + 3), out, w)
    fig_crosstab_bars(out, w)
    fig_calibration(np.random.default_rng(SEED + 4), out, w)


# ---------------------------------------------------------------- worked set: context
def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=2)


def _conninfo() -> str:
    """The libpq target: DATABASE_URL, or the PG* variables. Fails if neither is set."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    if os.environ.get("PGHOST") and os.environ.get("PGDATABASE"):
        return ""  # libpq reads PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
    _fail(
        "The worked figures read the DirtyDuck tutorial database, and neither"
        " DATABASE_URL nor PGHOST + PGDATABASE is set.\n"
        "Start it with `just tutorial-up`, point PG* (or DATABASE_URL) at it (port 5440"
        " by default, DIRTYDUCK_PG_PORT), and run again. `--illustrative` needs no"
        " database."
    )


def _query(pool: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A read the dashboard has no route for, on the same pool (dict rows)."""
    with pool.connection() as conn:
        return list(conn.execute(sql, params).fetchall())


@dataclass
class Model:
    model_id: int
    train_end_time: date
    dates: list[date]


@dataclass
class Ctx:
    pool: Any
    routes: Any
    experiment_hash: str
    cut: str
    best_group: int
    second_group: int
    groups: dict[int, dict[str, Any]]
    models: dict[int, list[Model]]
    before_group: int = 0

    def cut_pct(self) -> float | None:
        """The primary cut as a share (``10_pct`` -> 0.1); None for a top-k cut."""
        return (
            float(self.cut.split("_")[0]) / 100 if self.cut.endswith("_pct") else None
        )

    def latest(self, group: int) -> Model:
        return self.models[group][-1]

    def peer(self, group: int, model: Model) -> Model:
        """The model of ``group`` trained on the same window as ``model``."""
        return next(
            x for x in self.models[group] if x.train_end_time == model.train_end_time
        )


def _group_models(
    ctx_routes: Any, pool: Any, group: int, exp: str, cut: str
) -> list[Model]:
    detail = ctx_routes.model_group_detail(
        group, metric="precision@", parameter=cut, experiment_hash=exp, pool=pool
    )
    by_model: dict[int, list[date]] = {}
    for row in detail["metric_over_time"]:
        by_model.setdefault(int(row["model_id"]), []).append(row["as_of_date"])
    models = [
        Model(
            int(m["model_id"]),
            m["train_end_time"],
            sorted(by_model.get(int(m["model_id"]), [])),
        )
        for m in detail["models"]
    ]
    return sorted(models, key=lambda m: (m.train_end_time, m.model_id))


def _context(config: Path) -> Ctx:
    conninfo = _conninfo()
    try:
        from triage.dashboard import routes
    except ImportError as exc:  # the dashboard extra (fastapi) is not installed
        _fail(
            f"Cannot import the dashboard routes ({exc}). The worked figures read through"
            " them; install the extras with `uv sync --all-extras`."
        )
    from triage.adapters.run import experiment_hash_for
    from triage.util.db import connection_pool

    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    cut = str((cfg.get("bias_config") or {}).get("parameter") or "")
    if not cut:
        _fail(f"{config} has no bias_config.parameter, the guides' primary cut.")
    exp = experiment_hash_for(cfg)
    target = conninfo or (
        f"{os.environ.get('PGHOST')}:{os.environ.get('PGPORT', '5432')}"
        f"/{os.environ.get('PGDATABASE')}"
    )
    try:
        pool = connection_pool(conninfo, min_size=1, max_size=2, timeout=10)
        with pool.connection() as conn:
            conn.execute("select 1")
    except Exception as exc:  # noqa: BLE001 — report and stop: nothing to draw from
        _fail(
            f"Cannot reach the tutorial database ({exc.__class__.__name__}: {exc}).\n"
            "Start it with `just tutorial-up` and check that PG* or DATABASE_URL point at"
            " it (the DirtyDuck default is port 5440)."
        )
    if not _query(
        pool,
        "select 1 as ok from triage.experiments where experiment_hash = %(h)s",
        {"h": exp},
    ):
        _fail(
            f"Experiment {exp[:12]}… ({config.relative_to(REPO)}) is not in this database."
            "\nRun it first (the figures never run an experiment):\n"
            f"  uv run triage run {config.relative_to(REPO)} --project-path"
            ' "$HOME/.cache/triage-pg/dirtyduck-guides"'
        )
    typer.echo(f"Worked figures: experiment {exp[:12]}… at {target or 'DATABASE_URL'}")
    aud = routes.audition(
        exp, metric="precision@", parameter=cut, rule="best_average_value", pool=pool
    )
    if aud.get("empty"):
        _fail(f"Audition has nothing to rank yet: {aud.get('reason')}")
    best = int(aud["pick"])
    second = next(
        int(r["model_group_id"])
        for r in aud["ranking"]
        if int(r["model_group_id"]) != best
    )
    groups = {
        int(g["model_group_id"]): dict(g)
        for g in routes.experiment_model_groups(exp, pool=pool)
    }
    labels = _query(
        pool,
        "select distinct m.model_group_id, r.plan -> 'attempt' ->> 'feature_group' as fg"
        " from triage.models m join triage.runs r using (run_id)"
        " where r.experiment_hash = %(h)s",
        {"h": exp},
    )
    for row in labels:
        groups[int(row["model_group_id"])]["feature_group"] = row["fg"]
    avg = {int(r["model_group_id"]): r for r in aud["ranking"]}
    for gid, g in groups.items():
        g["avg_value"] = (avg.get(gid) or {}).get("avg_value")
    models = {gid: _group_models(routes, pool, gid, exp, cut) for gid in groups}
    ctx = Ctx(pool, routes, exp, cut, best, second, groups, models)
    # "Before": the best group's estimator and hyperparameters on experiment.yaml's
    # features (the run without the violation counts).
    ctx.before_group = _feature_group_peer(ctx, best, "leave-one-out:severity")
    _require_diagnostics(ctx)
    return ctx


def _require_diagnostics(ctx: Ctx) -> None:
    """The error trees and crosstabs must exist for every model the figures read."""
    missing: list[str] = []
    for gid in (ctx.best_group, ctx.second_group, ctx.before_group):
        for m in ctx.models[gid]:
            have = _query(
                ctx.pool,
                "select (select count(*) from triage.error_analysis where model_id = %(m)s"
                "          and parameter = %(p)s and error_kind = 'confusion') as trees,"
                "       (select count(*) from triage.crosstabs where model_id = %(m)s"
                "          and parameter = %(p)s) as xtabs",
                {"m": m.model_id, "p": ctx.cut},
            )[0]
            if gid != ctx.second_group and not have["trees"]:
                missing.append(
                    f"uv run triage postmodel error-tree {m.model_id} -p {ctx.cut} --kind all"
                )
            if not have["xtabs"]:
                missing.append(
                    f"uv run triage postmodel crosstabs {m.model_id} -p {ctx.cut}"
                )
    if missing:
        _fail(
            "The worked figures need the diagnostics of the audition pick"
            f" ({ctx.best_group}), the runner-up ({ctx.second_group}) and the pick's peer"
            f" on experiment.yaml's features ({ctx.before_group}). Run:\n  "
            + "\n  ".join(missing)
        )


# ---------------------------------------------------------------- worked set: figures
def _short_type(model_type: str) -> str:
    return {
        "RandomForestClassifier": "random forest",
        "DecisionTreeClassifier": "decision tree",
        "ScaledLogisticRegression": "logistic regression",
        "DummyClassifier": "constant prior",
    }.get(model_type.rsplit(".", 1)[-1], model_type.rsplit(".", 1)[-1])


def _group_name(ctx: Ctx, gid: int) -> str:
    g = ctx.groups[gid]
    fg = str(g.get("feature_group") or "")
    features = {
        "all": "all features",
        "leave-one-out:severity": "experiment.yaml's features",
        "leave-one-out:base": "violation counts only",
    }.get(fg, fg)
    return f"group {gid}: {_short_type(str(g['model_type']))}, {features}"


def _subset_hashes(ctx: Ctx) -> dict[str, str]:
    return {
        str(s["name"]): str(s["subset_hash"])
        for s in ctx.routes.experiment_subsets(ctx.experiment_hash, pool=ctx.pool)
    }


def _save_plot(result: Any, name: str, out_dir: Path, w: Written, note: str) -> None:
    """Write a :mod:`triage.plots` figure as ``name``, with ``note`` wrapped under it.

    The figure is exactly what ``triage plot`` writes; the note is the guides' provenance
    line and the only thing added.
    """
    from triage.plots.style import INK as PLOT_INK
    from triage.plots.style import style as plot_style

    fig = result.figure
    with plot_style():
        fig.text(
            0.005,
            0.0,
            textwrap.fill(note, int(fig.get_figwidth() * 15.5)),
            ha="left",
            va="top",
            fontsize=8,
            color=PLOT_INK,
        )
    path = result.save(out_dir / name)
    w.files.append(path)
    w.data[name.removesuffix(".svg")] = {"summary": result.summary}
    typer.echo(f"  wrote {path.relative_to(REPO)} — {result.summary}")


def fig_worked_curve(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import curve

    m = ctx.latest(ctx.best_group)
    d = m.dates[-1]
    result = curve(ctx.pool, m.model_id, as_of_date=d)
    rows = result.data["everyone"]
    _save_plot(
        result,
        "worked-curve.svg",
        out,
        w,
        f"DirtyDuck, {_group_name(ctx, ctx.best_group)}; {rows[-1]['k']:,} scored"
        " facilities.",
    )


def fig_worked_curve_subsets(
    ctx: Ctx, out: Path, w: Written, names: Sequence[str], name: str
) -> None:
    """The best model's curve for everyone and for each Subset, each re-ranked inside."""
    from triage.plots import curve

    m = ctx.latest(ctx.best_group)
    d = m.dates[-1]
    hashes = _subset_hashes(ctx)
    result = curve(
        ctx.pool, m.model_id, as_of_date=d, subsets=[n for n in names if n in hashes]
    )
    _save_plot(
        result,
        name,
        out,
        w,
        f"DirtyDuck, model {m.model_id} at {d}. Each Subset is re-ranked inside itself"
        " (triage.model_threshold_curve with p_subset_hash).",
    )


def fig_worked_curve_dates(
    ctx: Ctx,
    out: Path,
    w: Written,
    subset: str = "",
    name: str = "worked-curve-dates.svg",
) -> None:
    """The best group's curve on every test date, for everyone or for one Subset."""
    from triage.plots import curve_dates

    result = curve_dates(ctx.pool, ctx.best_group, subset=subset)
    _save_plot(
        result,
        name,
        out,
        w,
        f"DirtyDuck, {_group_name(ctx, ctx.best_group)}: three models, each tested on two"
        " dates three months apart. Adjacent dates of one model share the model; a jump"
        " between them is a change in the data.",
    )


def fig_worked_curve_floor(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import curve

    best = ctx.groups[ctx.best_group]
    floor = next(
        gid
        for gid, g in ctx.groups.items()
        if str(g["model_type"]).endswith("DummyClassifier")
        and list(g["feature_list"]) == list(best["feature_list"])
    )
    m = ctx.latest(ctx.best_group)
    d = m.dates[-1]
    fm = ctx.peer(floor, m)
    result = curve(ctx.pool, m.model_id, as_of_date=d, compare=fm.model_id)
    _save_plot(
        result,
        "worked-curve-floor.svg",
        out,
        w,
        f"DirtyDuck, {_group_name(ctx, ctx.best_group)} against"
        f" {_group_name(ctx, floor)}. The floor scores every facility the same, so its"
        " list is the tie order: precision stays at the base rate.",
    )


def fig_worked_error_rules(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import error_rules

    m = ctx.latest(ctx.best_group)
    d = m.dates[-1]
    result = error_rules(ctx.pool, m.model_id, as_of_date=d, parameter=ctx.cut, top=6)
    _save_plot(
        result,
        "worked-error-rules.svg",
        out,
        w,
        f"DirtyDuck, model {m.model_id} at {d}, cut {ctx.cut}: the six leaves with the"
        " most errors of each tree (depth 3, at least 30 rows per leaf).",
    )


def fig_worked_error_recurrence(ctx: Ctx, out: Path, w: Written) -> None:
    """fn rule recurrence for the best group and for its peer on fewer features."""
    from triage.plots import recurrence

    for gid, name in (
        (ctx.best_group, "worked-error-recurrence.svg"),
        (ctx.before_group, "worked-error-recurrence-before.svg"),
    ):
        result = recurrence(ctx.pool, gid, ctx.cut, kind="fn")
        _save_plot(
            result,
            name,
            out,
            w,
            f"DirtyDuck, {_group_name(ctx, gid)}. Rules are matched by signature"
            " (features and split directions, without thresholds) across the group's"
            " test dates.",
        )


def fig_worked_suspicious(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import leaves

    result = leaves(ctx.pool, ctx.best_group, ctx.cut)
    _save_plot(
        result,
        "worked-suspicious-leaves.svg",
        out,
        w,
        f"DirtyDuck, every leaf of {_group_name(ctx, ctx.best_group)} across its"
        f" six test dates at {ctx.cut}.",
    )


def fig_worked_crosstabs_ratio(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import crosstabs

    m = ctx.latest(ctx.best_group)
    d = m.dates[-1]
    result = crosstabs(ctx.pool, m.model_id, parameter=ctx.cut, as_of_date=d, top=15)
    _save_plot(
        result,
        "worked-crosstabs-ratio.svg",
        out,
        w,
        "DirtyDuck, the 15 features with the largest |log ratio| of means. Labels: mean"
        " on the list vs mean on the rest.",
    )


# The rows the crosstabs guide reads, one per shape it names: the model's own signal,
# an over-represented category, and a group attribute the model never uses.
NONZERO_FEATURES = (
    "MAX(inspections.n_critical)",
    "STDDEV(inspections.n_critical)",
    "facilities.facility_type=restaurant",
    "facilities.facility_type=grocery store",
    "facilities.zip_code=60622",
    "facilities.zip_code=60614",
)


def fig_worked_crosstabs_nonzero(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import crosstabs

    m = ctx.latest(ctx.best_group)
    d = m.dates[-1]
    result = crosstabs(
        ctx.pool,
        m.model_id,
        parameter=ctx.cut,
        as_of_date=d,
        stat="nonzero_rate",
        features=NONZERO_FEATURES,
    )
    _save_plot(
        result,
        "worked-crosstabs-nonzero.svg",
        out,
        w,
        "DirtyDuck, triage.crosstabs stat nonzero_rate: two critical-violation features"
        " the model uses, then two facility types and two zip codes it does not use.",
    )


def fig_worked_crosstabs_two_groups(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import crosstabs

    a = ctx.latest(ctx.best_group)
    b = ctx.peer(ctx.second_group, a)
    d = a.dates[-1]
    result = crosstabs(
        ctx.pool,
        a.model_id,
        parameter=ctx.cut,
        as_of_date=d,
        compare=b.model_id,
        top=12,
    )
    _save_plot(
        result,
        "worked-crosstabs-two-groups.svg",
        out,
        w,
        f"DirtyDuck, model {a.model_id} ({_group_name(ctx, ctx.best_group)}) and model"
        f" {b.model_id} ({_group_name(ctx, ctx.second_group)}): the twelve features the"
        " first list separates most, read for both lists.",
    )


def fig_worked_crosstabs_dates(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import crosstabs_dates

    result = crosstabs_dates(ctx.pool, ctx.best_group, parameter=ctx.cut, top=4)
    _save_plot(
        result,
        "worked-crosstabs-dates.svg",
        out,
        w,
        f"DirtyDuck, {_group_name(ctx, ctx.best_group)}: the four features its latest"
        " list separates most, on every test date.",
    )


def fig_worked_overlap(ctx: Ctx, out: Path, w: Written) -> None:
    from triage.plots import overlap

    result = overlap(ctx.pool, ctx.best_group, ctx.second_group, ctx.cut)
    _save_plot(
        result,
        "worked-list-overlap.svg",
        out,
        w,
        f"DirtyDuck, triage.list_overlap: {_group_name(ctx, ctx.best_group)} against"
        f" {_group_name(ctx, ctx.second_group)}.",
    )


def fig_worked_calibration(ctx: Ctx, out: Path, w: Written) -> None:
    a = ctx.latest(ctx.best_group)
    b = ctx.peer(ctx.second_group, a)
    fig, ax = plt.subplots(figsize=(5.4, 4.2))
    ax.plot([0, 1], [0, 1], color=INK, linewidth=0.8, linestyle=BASE_DASH)
    data: dict[str, Any] = {}
    for model, gid, colour in (
        (a, ctx.best_group, BLUE),
        (b, ctx.second_group, ORANGE),
    ):
        cal = ctx.routes.model_calibration(
            model.model_id, split_kind="test", as_of_date=None, pool=ctx.pool
        )
        dec = cal.get("deciles") or []
        ax.plot(
            [r["avg_score"] for r in dec],
            [r["realized_rate"] for r in dec],
            color=colour,
            marker="o",
            markersize=4,
            label=f"model {model.model_id} ({_short_type(str(ctx.groups[gid]['model_type']))})",
        )
        data[str(model.model_id)] = {
            "as_of_date": str(cal.get("as_of_date")),
            "deciles": dec,
        }
    ax.set_xlim(0, 0.7)
    ax.set_ylim(0, 0.7)
    ax.set_xlabel("mean score in the decile")
    ax.set_ylabel("observed failure rate")
    ax.set_title("Calibration deciles")
    ax.legend(loc="upper left", fontsize=8)
    _save(
        fig,
        "worked-calibration.svg",
        out,
        w,
        "DirtyDuck, triage.monitoring_calibration at each model's latest test date.",
    )
    w.data["worked-calibration"] = data


def fig_worked_histogram(ctx: Ctx, out: Path, w: Written) -> None:
    m = ctx.latest(ctx.best_group)
    rows = ctx.routes.model_histogram(m.model_id, bins=20, pool=ctx.pool)
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    lo = [float(r["lo"]) for r in rows]
    width = 1.0 / 20
    ax.bar(
        lo,
        [int(r["n"]) for r in rows],
        width=width * 0.92,
        align="edge",
        color=BLUE,
        label="every scored facility",
    )
    ax.bar(
        lo,
        [int(r["n_pos"]) for r in rows],
        width=width * 0.92,
        align="edge",
        color=ORANGE,
        label="labeled failures among them",
    )
    ax.set_xlim(0, 1)
    ax.set_xlabel("score")
    ax.set_ylabel("facilities")
    ax.set_title(f"Score histogram: model {m.model_id}, both test dates")
    ax.legend(loc="upper right")
    ax.grid(axis="x", visible=False)
    _save(
        fig,
        "worked-score-histogram.svg",
        out,
        w,
        "DirtyDuck, triage.model_score_histogram (20 bins over [0, 1]).",
    )
    w.data["worked-score-histogram"] = {"model_id": m.model_id, "bins": rows}


def fig_worked_subset_metrics(ctx: Ctx, out: Path, w: Written) -> None:
    hashes = {"everyone": "", **_subset_hashes(ctx)}
    names = ["everyone", "restaurant", "grocery_store", "school"]
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    data: dict[str, Any] = {}
    for name, colour in zip(names, SERIES, strict=True):
        if name not in hashes:
            continue
        ev = ctx.routes.evaluations(
            ctx.experiment_hash,
            metric="precision@",
            subset_hash=hashes[name],
            pool=ctx.pool,
        )
        ev = sorted(
            (
                r
                for r in ev
                if int(r["model_group_id"]) == ctx.best_group
                and r["parameter"] == ctx.cut
            ),
            key=lambda r: r["as_of_date"],
        )
        xs = [str(r["as_of_date"])[2:7] for r in ev]
        ax.plot(
            xs,
            [r["value"] for r in ev],
            color=colour,
            marker="o",
            markersize=4,
            label=name.replace("_", " "),
        )
        data[name] = [
            {
                "as_of_date": str(r["as_of_date"]),
                "value": r["value"],
                "num_labeled": r["num_labeled"],
                "num_positive": r["num_positive"],
            }
            for r in ev
        ]
    ax.set_ylim(0, 0.7)
    ax.set_xlabel("test date (yy-mm)")
    ax.set_ylabel(f"precision@{ctx.cut}")
    ax.set_title("Subset metrics: each subset ranked inside itself")
    ax.legend(loc="lower right", ncols=2)
    _save(
        fig,
        "worked-subset-metrics.svg",
        out,
        w,
        f"DirtyDuck, {_group_name(ctx, ctx.best_group)}; triage.evaluations by"
        " subset_hash.",
    )
    w.data["worked-subset-metrics"] = data


def _feature_group_peer(ctx: Ctx, gid: int, feature_group: str) -> int:
    """The group with the same estimator and hyperparameters in another run."""
    g = ctx.groups[gid]
    return next(
        other
        for other, o in ctx.groups.items()
        if o.get("feature_group") == feature_group
        and o["model_type"] == g["model_type"]
        and o["hyperparameters"] == g["hyperparameters"]
    )


def fig_worked_feature_ablation(ctx: Ctx, out: Path, w: Written) -> None:
    order = ["leave-one-out:severity", "all", "leave-one-out:base"]
    names = {
        "leave-one-out:severity": "experiment.yaml's features",
        "all": "+ violation counts",
        "leave-one-out:base": "violation counts only",
    }
    types = sorted({str(g["model_type"]) for g in ctx.groups.values()}, key=_short_type)
    fig, ax = plt.subplots(figsize=(7.6, 3.0))
    ys = np.arange(len(types))[::-1]
    data: dict[str, dict[str, Any]] = {}
    for y, t in zip(ys, types, strict=True):
        vals = []
        for fg in order:
            gid = next(
                (
                    k
                    for k, g in ctx.groups.items()
                    if g["model_type"] == t and g.get("feature_group") == fg
                ),
                None,
            )
            v = float(ctx.groups[gid]["avg_value"]) if gid is not None else float("nan")
            vals.append(v)
            data.setdefault(_short_type(t), {})[fg] = {"group": gid, "avg": v}
        ax.plot([min(vals), max(vals)], [y, y], color=INK, linewidth=1.0, alpha=0.6)
    for fg, colour, marker in zip(order, SERIES, ("o", "s", "D"), strict=False):
        xs = [data[_short_type(t)][fg]["avg"] for t in types]
        ax.scatter(
            xs,
            ys,
            s=44,
            color=colour,
            marker=marker,
            edgecolors="none",
            label=names[fg],
            zorder=3,
        )
    for y, t in zip(ys, types, strict=True):
        best = max(data[_short_type(t)].values(), key=lambda d: d["avg"])
        ax.annotate(
            f"{best['avg']:.3f}",
            (best["avg"], y),
            xytext=(7, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
        )
    ax.set_yticks(ys, [_short_type(t) for t in types])
    ax.set_xlabel(f"mean precision@{ctx.cut} over six test dates")
    ax.set_title("Adding a feature, before and after: three runs of one experiment")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
    ax.grid(axis="y", visible=False)
    _save(
        fig,
        "worked-feature-ablation.svg",
        out,
        w,
        "DirtyDuck, the audition's means (feature_groups, strategies all +"
        " leave-one-out). The constant prior scores at the base rate in all three.",
    )
    w.data["worked-feature-ablation"] = data


def fig_worked_bias(ctx: Ctx, out: Path, w: Written) -> None:
    m = ctx.latest(ctx.best_group)
    d = m.dates[-1]
    rows = ctx.routes.bias(ctx.experiment_hash, model_id=m.model_id, pool=ctx.pool)
    if isinstance(rows, dict):
        _fail("No bias metrics: the guides config's bias_config did not run.")
    rows = [r for r in rows if r["parameter"] == ctx.cut and r["as_of_date"] == d]
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        by.setdefault(str(r["attribute_value"]), {})[str(r["metric"])] = r
    zips = sorted(by, key=lambda z: (-float(by[z]["group_size"]["value"]), z))[:12][
        ::-1
    ]
    ref = str(by[zips[-1]]["fdr"]["ref_group_value"])
    ref_fdr = float(by[ref]["fdr"]["value"]) if ref in by else float("nan")
    tau = float(by[zips[-1]]["fdr"]["tau"] or 0.8)
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    ypos = np.arange(len(zips))
    fdr = [float(by[z]["fdr"]["value"] or 0) for z in zips]
    ax.barh(ypos, fdr, height=0.55, color=BLUE)
    ax.axvspan(
        tau * ref_fdr, min(ref_fdr / tau, 1.0), color=AQUA, alpha=0.15, linewidth=0
    )
    ax.axvline(ref_fdr, color=INK, linewidth=1.0, linestyle=BASE_DASH)
    outside = [z for z in zips if by[z]["fdr"]["passes_fairness"] is False]
    for yv, z in zip(ypos, zips, strict=True):
        sel = by[z].get("num_selected", {}).get("value")
        ax.annotate(
            f"{by[z]['fdr']['value']:.2f}  ({int(sel or 0)} selected)",
            (fdr[int(yv)], float(yv)),
            xytext=(4, 0),
            textcoords="offset points",
            va="center",
            fontsize=7.5,
        )
    ax.scatter(
        [fdr[zips.index(z)] for z in outside],
        [zips.index(z) for z in outside],
        marker="D",
        s=22,
        color=ORANGE,
        zorder=3,
        label=f"outside the τ = {tau} band",
    )
    ax.set_yticks(
        ypos, [f"{z}{' (reference)' if z == ref else ''}" for z in zips], fontsize=8
    )
    ax.set_xlim(0, 1.15)
    ax.set_xlabel("false discovery rate among the selected (labeled)")
    ax.set_title(f"FDR by zip code: model {m.model_id} at {d}, top {ctx.cut}")
    ax.legend(loc="lower right")
    ax.grid(axis="y", visible=False)
    _save(
        fig,
        "worked-bias-fdr.svg",
        out,
        w,
        "DirtyDuck, triage.bias_metrics; the twelve largest zip codes. The band is"
        " the reference group's FDR × [τ, 1/τ].",
    )
    w.data["worked-bias-fdr"] = {
        "model_id": m.model_id,
        "as_of_date": str(d),
        "reference": ref,
        "ref_fdr": ref_fdr,
        "tau": tau,
        "zips": {
            z: {
                k: by[z][k]["value"]
                for k in ("group_size", "num_selected", "fdr", "precision")
                if k in by[z]
            }
            | {
                "fdr_disparity": by[z]["fdr"]["disparity"],
                "passes": by[z]["fdr"]["passes_fairness"],
            }
            for z in zips[::-1]
        },
    }


def worked(config: Path, out: Path, w: Written) -> None:
    ctx = _context(config)
    best, second = ctx.best_group, ctx.second_group
    typer.echo(
        f"  best group {best} ({_group_name(ctx, best)}), runner-up {second}"
        f" ({_group_name(ctx, second)}), primary cut {ctx.cut}"
    )
    w.data["context"] = {
        "experiment_hash": ctx.experiment_hash,
        "cut": ctx.cut,
        "best_group": best,
        "second_group": second,
        "groups": {
            gid: {
                "name": _group_name(ctx, gid),
                "avg_value": g.get("avg_value"),
                "models": [m.model_id for m in ctx.models[gid]],
            }
            for gid, g in sorted(ctx.groups.items())
        },
    }
    steps: list[Callable[[Ctx, Path, Written], None]] = [
        fig_worked_curve,
        fig_worked_curve_floor,
        fig_worked_curve_dates,
        fig_worked_error_rules,
        fig_worked_error_recurrence,
        fig_worked_suspicious,
        fig_worked_crosstabs_ratio,
        fig_worked_crosstabs_nonzero,
        fig_worked_crosstabs_two_groups,
        fig_worked_crosstabs_dates,
        fig_worked_overlap,
        fig_worked_calibration,
        fig_worked_histogram,
        fig_worked_subset_metrics,
        fig_worked_feature_ablation,
        fig_worked_bias,
    ]
    for step in steps:
        step(ctx, out, w)
    fig_worked_curve_subsets(
        ctx,
        out,
        w,
        ["restaurant", "grocery_store", "school"],
        "worked-curve-subsets.svg",
    )
    fig_worked_curve_dates(ctx, out, w, "zip_60618", "worked-curve-dates-zip.svg")
    fig_worked_curve_subsets(
        ctx, out, w, ["zip_60647", "zip_60618"], "worked-curve-fairness.svg"
    )
    ctx.pool.close()


@app.command()
def main(
    illustrative_only: bool = typer.Option(
        False, "--illustrative", help="Only the seeded figures; no database."
    ),
    worked_only: bool = typer.Option(
        False, "--worked", help="Only the figures read from the DirtyDuck tutorial DB."
    ),
    config: Path = typer.Option(
        GUIDES_CONFIG, help="The experiment config the worked figures come from."
    ),
    out_dir: Path = typer.Option(OUT_DIR, help="Where the SVGs are written."),
    data_json: Path | None = typer.Option(
        None, help="Also write the numbers behind the worked figures to this JSON file."
    ),
) -> None:
    """Write the guides' figures. With neither flag, both sets are written."""
    _style()
    both = not illustrative_only and not worked_only
    w = Written()
    if illustrative_only or both:
        illustrative(out_dir, w)
    if worked_only or both:
        worked(config.resolve(), out_dir, w)
    if data_json is not None:
        data_json.write_text(
            json.dumps(w.data, indent=2, default=str) + "\n", encoding="utf-8"
        )
        typer.echo(f"  wrote {data_json}")
    typer.echo(f"{len(w.files)} figure(s) written to {out_dir}")


if __name__ == "__main__":
    app()
