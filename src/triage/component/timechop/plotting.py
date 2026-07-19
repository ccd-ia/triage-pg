"""Temporal cross-validation visualization for a ``temporal_config`` (Timechop).

Renders the train/validation blocks a temporal config produces — each split's as-of dates, their
label windows, and the matrix spans — on a shared time axis, **one compact lane per split, most
recent on top**. Two backends share one design language:

* :func:`visualize_chops` — matplotlib, static (PNG/SVG/PDF). ``triage analyze-config --plot
  blocks.png``.
* :func:`visualize_chops_plotly` — plotly, interactive (hover for exact dates), a self-contained
  HTML. ``triage analyze-config --plot blocks.html`` (also embeddable in the dashboard).

The upstream DSSG plot stacked one subplot per split and drew train + validation in the *same*
random per-split color, which made it hard to read. This rewrite:

* gives **train and validation fixed, distinct, colorblind-safe colors** (Okabe–Ito), consistent
  across every split;
* uses **one lane per split** (train on the upper half-lane, validation on the lower), so a whole
  temporal design fits in one glance;
* draws each matrix's **as-of-date span** as a solid bar with per-as-of-date ticks, and its
  **label window** (the outcome lookahead) as a lighter extension — so the point-in-time boundary
  (no label leakage from validation back into train) is visible, not implied;
* marks the **feature-availability start** and keeps the axis to plain year/month ticks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

import matplotlib

matplotlib.use("Agg")  # headless: render to files, never a GUI window

import matplotlib.dates as mdates  # noqa: E402  (after backend selection)
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from triage.util.conf import convert_str_to_relativedelta  # noqa: E402

if TYPE_CHECKING:
    from triage.component.timechop.timechop import Timechop

# Fixed, colorblind-safe colors (Okabe–Ito), the same in every split so the eye tracks roles.
TRAIN_COLOR = "#0072B2"  # blue      — training matrix
TEST_COLOR = "#D55E00"  # vermillion  — validation (test) matrix
FEATURE_COLOR = "#009E73"  # green    — feature-availability boundary
_HALF = 0.34  # half-lane offset (train above the split centerline, validation below)
_BAR = 0.30  # bar thickness


def _label_horizon(as_of_times: Sequence[Any], timespan: str):
    """Where the label window reaching from the *last* as-of date ends (the outcome horizon)."""
    return max(as_of_times) + convert_str_to_relativedelta(timespan)


def _bar(ax, start, end, y, color, *, alpha=1.0):
    """A horizontal bar from ``start`` to ``end`` (datetimes) centered on lane ``y``."""
    x0, x1 = mdates.date2num(start), mdates.date2num(end)
    ax.barh(
        y, x1 - x0, left=x0, height=_BAR, color=color, alpha=alpha, edgecolor="none"
    )


def _ticks(ax, dates, y, color):
    """Per-as-of-date markers: white-filled, color-ringed circles so they read both ON a solid
    span bar AND on empty background (a single-date validation matrix has a zero-width bar).
    """
    ax.plot(
        [mdates.date2num(d) for d in dates],
        [y] * len(dates),
        marker="o",
        linestyle="none",
        markersize=4,
        markerfacecolor="white",
        markeredgecolor=color,
        markeredgewidth=1.0,
    )


def visualize_chops(
    chopper: "Timechop",
    *,
    save_target=None,
    show_label_windows: bool = True,
) -> None:
    """Render a Timechop config's temporal cross-validation blocks with matplotlib.

    Args:
        chopper: a configured :class:`~triage.component.timechop.timechop.Timechop`.
        save_target: path/file to save to (format from the extension). ``None`` → interactive show.
        show_label_windows: draw the label-window lookahead bars (the outcome horizon).
    """
    chops = list(chopper.chop_time())
    chops.reverse()  # most recent split on top
    n = len(chops)

    fig, ax = plt.subplots(figsize=(14, 1.5 * n + 1.6))

    for row, chop in enumerate(chops):
        y = n - 1 - row  # row 0 (most recent) at the top
        train, test = chop["train_matrix"], chop["test_matrices"][0]
        train_aost, test_aost = train["as_of_times"], test["as_of_times"]
        train_span = train["training_label_timespan"]
        test_span = test["test_label_timespan"]

        y_train, y_test = y + _HALF / 2, y - _HALF / 2

        # as-of-date spans (solid) + per-date ticks
        _bar(ax, min(train_aost), max(train_aost), y_train, TRAIN_COLOR)
        _bar(ax, min(test_aost), max(test_aost), y_test, TEST_COLOR)
        _ticks(ax, train_aost, y_train, TRAIN_COLOR)
        _ticks(ax, test_aost, y_test, TEST_COLOR)

        # label-window lookahead (lighter extension past the last as-of date)
        if show_label_windows:
            _bar(
                ax,
                max(train_aost),
                _label_horizon(train_aost, train_span),
                y_train,
                TRAIN_COLOR,
                alpha=0.25,
            )
            _bar(
                ax,
                max(test_aost),
                _label_horizon(test_aost, test_span),
                y_test,
                TEST_COLOR,
                alpha=0.25,
            )

        # feature-availability start (where knowable data begins for this split)
        ax.axvline(
            float(mdates.date2num(chop["feature_start_time"])),
            color=FEATURE_COLOR,
            linestyle=":",
            linewidth=1.0,
            alpha=0.7,
        )

        ax.text(
            float(mdates.date2num(min(train_aost))),
            y + _HALF + 0.06,
            f"Split {row + 1}",
            va="bottom",
            ha="left",
            fontsize=9,
            color="#333333",
        )
        ax.text(
            float(mdates.date2num(_label_horizon(test_aost, test_span))),
            y,
            f"  train label {train_span} · val label {test_span}  ·  "
            f"{len(train_aost)} train / {len(test_aost)} val as-of dates",
            va="center",
            ha="left",
            fontsize=7.5,
            color="#777777",
        )

    ax.set_yticks([])
    ax.set_ylim(-0.7, n - 0.1)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_minor_locator(mdates.MonthLocator())
    ax.grid(axis="x", which="major", color="#e6e6e6", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_xlabel("time")
    ax.set_title(
        "Timechop — temporal cross-validation blocks (most recent split on top)"
    )

    handles = [
        Patch(color=TRAIN_COLOR, label="Train matrix (as-of dates)"),
        Patch(color=TRAIN_COLOR, alpha=0.25, label="Train label window"),
        Patch(color=TEST_COLOR, label="Validation matrix (as-of dates)"),
        Patch(color=TEST_COLOR, alpha=0.25, label="Validation label window"),
        Line2D([0], [0], color=FEATURE_COLOR, linestyle=":", label="Feature start"),
    ]
    ax.legend(
        handles=handles,
        loc="upper left",
        bbox_to_anchor=(1.005, 1.0),
        fontsize=8,
        frameon=False,
    )
    fig.tight_layout()

    if save_target:
        fig.savefig(save_target, bbox_inches="tight", dpi=150)
        plt.close(fig)
    else:
        plt.show()


def visualize_chops_plotly(chopper: "Timechop", *, save_target=None):
    """Interactive version of :func:`visualize_chops` (plotly).

    Same one-lane-per-split design with hover tooltips for exact dates. ``save_target`` ending in
    ``.html`` writes a self-contained page (embeddable in the dashboard); otherwise ``fig.show()``.
    """
    import plotly.graph_objects as go

    chops = list(chopper.chop_time())
    chops.reverse()
    n = len(chops)
    fig = go.Figure()

    def _shape(x0, x1, y, color, opacity):
        fig.add_shape(
            type="rect",
            x0=x0,
            x1=x1,
            y0=y - _BAR / 2,
            y1=y + _BAR / 2,
            fillcolor=color,
            opacity=opacity,
            line_width=0,
            layer="below",
        )

    for row, chop in enumerate(chops):
        y = n - 1 - row
        train, test = chop["train_matrix"], chop["test_matrices"][0]
        train_aost, test_aost = train["as_of_times"], test["as_of_times"]
        train_span = train["training_label_timespan"]
        test_span = test["test_label_timespan"]
        y_train, y_test = y + _HALF / 2, y - _HALF / 2

        _shape(min(train_aost), max(train_aost), y_train, TRAIN_COLOR, 1.0)
        _shape(
            max(train_aost),
            _label_horizon(train_aost, train_span),
            y_train,
            TRAIN_COLOR,
            0.25,
        )
        _shape(min(test_aost), max(test_aost), y_test, TEST_COLOR, 1.0)
        _shape(
            max(test_aost),
            _label_horizon(test_aost, test_span),
            y_test,
            TEST_COLOR,
            0.25,
        )

        fig.add_trace(
            go.Scatter(
                x=list(train_aost),
                y=[y_train] * len(train_aost),
                mode="markers",
                marker=dict(
                    color="white",
                    size=7,
                    symbol="circle",
                    line=dict(color=TRAIN_COLOR, width=1.5),
                ),
                name="Train as-of date",
                legendgroup="train",
                showlegend=(row == 0),
                hovertemplate="Split %d · train as-of %%{x|%%Y-%%m-%%d}<extra></extra>"
                % (row + 1),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=list(test_aost),
                y=[y_test] * len(test_aost),
                mode="markers",
                marker=dict(
                    color="white",
                    size=7,
                    symbol="circle",
                    line=dict(color=TEST_COLOR, width=1.5),
                ),
                name="Validation as-of date",
                legendgroup="val",
                showlegend=(row == 0),
                hovertemplate="Split %d · val as-of %%{x|%%Y-%%m-%%d}<extra></extra>"
                % (row + 1),
            )
        )
        fig.add_annotation(
            x=min(train_aost),
            y=y + _HALF + 0.05,
            text=f"Split {row + 1}",
            showarrow=False,
            xanchor="left",
            yanchor="bottom",
            font=dict(size=10, color="#333333"),
        )

    # legend proxies for the matrix bars (shapes don't appear in the legend)
    for name, color, group in [
        ("Train matrix", TRAIN_COLOR, "train"),
        ("Validation matrix", TEST_COLOR, "val"),
    ]:
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                marker=dict(color=color, size=10, symbol="square"),
                name=name,
                legendgroup=group,
            )
        )

    fig.update_layout(
        title="Timechop — temporal cross-validation blocks (most recent split on top)",
        height=140 * n + 140,
        width=1000,
        template="plotly_white",
        yaxis=dict(showticklabels=False, range=[-0.7, n - 0.1]),
        xaxis=dict(title="time"),
    )

    if save_target and str(save_target).lower().endswith(".html"):
        fig.write_html(str(save_target), include_plotlyjs=True, full_html=True)
    elif save_target:
        fig.write_image(str(save_target))
    else:
        fig.show()
