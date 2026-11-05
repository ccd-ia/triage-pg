"""The look of every triage-pg figure, and how one is written to a file.

The colours are mid-tone steps that read on a white page and on a near-black one: the
eight categorical slots pass the lightness, chroma and adjacent colour-vision checks on
both surfaces, and :data:`INK`, the one text colour, reads at about 4:1 on both. Text is
laid out with the DejaVu Sans metrics bundled with matplotlib. An SVG keeps its text as
text, rendered in the reader's system sans, and is written with a fixed ``svg.hashsalt``
and no date, so two runs over the same rows give the same bytes.

The format follows the file's extension: ``.svg`` on a transparent background (for pages
with a light and a dark theme), ``.png`` and ``.pdf`` on white (for reports).

The figures are built on :class:`matplotlib.figure.Figure` directly, never through
``pyplot``, so drawing one touches no GUI backend and leaves no global figure behind.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, Optional

import matplotlib

# Headless by default. The guard leaves a session that already imported pyplot (a
# notebook, an interactive shell) on the backend it chose.
if "matplotlib.pyplot" not in sys.modules:
    matplotlib.use("Agg")

from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.ticker import FuncFormatter, PercentFormatter  # noqa: E402

#: The one text and reference colour.
INK = "#807e78"
#: The categorical slots, in their fixed order: blue, orange, aqua, yellow, magenta,
#: green, violet, red. A series takes the next slot; a ninth series is refused.
SERIES = (
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#e66767",
)
BLUE, ORANGE, AQUA, YELLOW = SERIES[:4]
#: The most series one figure draws (Subsets, test dates, compared models, features).
MAX_SERIES = len(SERIES)
#: The file formats a figure is written in, by extension.
FORMATS = ("svg", "png", "pdf")
#: The dash of a reference line (a base rate, a population rate), never a gridline.
BASE_DASH = (0, (4, 3))
SYSTEM_SANS = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"

RC: dict[str, Any] = {
    "svg.hashsalt": "triage-pg",
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
    "axes.facecolor": "none",
    "figure.facecolor": "none",
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
    "savefig.dpi": 150,
}


@contextmanager
def style() -> Iterator[None]:
    """The triage-pg rcParams, for the duration of a build or a save.

    The figure is styled inside this context rather than through the global rcParams,
    so drawing a triage-pg figure does not restyle the caller's own plots.
    """
    with matplotlib.rc_context(RC):
        yield


def output_format(path: str | Path) -> str:
    """The format a figure is written in, read from the extension of ``path``.

    Raises:
        ValueError: the extension is not one of :data:`FORMATS`.
    """
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix not in FORMATS:
        raise ValueError(
            f"Cannot write {str(path)!r}: the format follows the extension, and"
            f" triage-pg writes {', '.join('.' + f for f in FORMATS)} only."
        )
    return suffix


@dataclass
class PlotResult:
    """A drawn figure and the rows it drew.

    Attributes:
        figure: the matplotlib figure.
        data: the rows drawn, keyed by series (a Subset, a test date, a model, a kind
            of leaf). Each value is what :mod:`triage.diagnostics.readers` returned for
            that series, unchanged, so the figure can be checked against the database.
        summary: one line with the figure's main numbers, as ``triage plot`` prints it.
    """

    figure: Figure
    data: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def save(self, path: str | Path) -> Path:
        """Write the figure to ``path``, in the format its extension names.

        ``.svg`` is written on a transparent background, with no date or creator in its
        metadata, so the same rows give the same bytes. ``.png`` and ``.pdf`` are
        written on white.

        Returns:
            The path written.

        Raises:
            ValueError: the extension is not ``.svg``, ``.png`` or ``.pdf``.
        """
        target = Path(path)
        fmt = output_format(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        common: dict[str, Any] = {"format": fmt, "bbox_inches": "tight"}
        common["pad_inches"] = 0.08
        with style():
            if fmt == "svg":
                buffer = io.BytesIO()
                self.figure.savefig(
                    buffer,
                    transparent=True,
                    metadata={"Date": None, "Creator": None},
                    **common,
                )
                text = buffer.getvalue().decode("utf-8")
                # laid out with DejaVu Sans metrics, rendered in the reader's sans
                text = text.replace("'DejaVu Sans', sans-serif", SYSTEM_SANS)
                target.write_text(text, encoding="utf-8")
            elif fmt == "pdf":
                self.figure.savefig(
                    target,
                    facecolor="white",
                    metadata={"CreationDate": None, "Creator": None, "Producer": None},
                    **common,
                )
            else:
                self.figure.savefig(target, facecolor="white", **common)
        return target


class NothingToDraw(LookupError):
    """The database holds none of the rows a figure draws.

    The message names the command that creates them.
    """


def check_series(count: int, what: str, narrower: str) -> None:
    """Refuse more series than one figure keeps legible.

    Args:
        count: the series the figure would draw.
        what: what they are, for the message (``'curves'``, ``'features'``).
        narrower: how to ask for fewer.

    Raises:
        ValueError: ``count`` is above :data:`MAX_SERIES`.
    """
    if count > MAX_SERIES:
        raise ValueError(
            f"{count} {what} is more than one figure draws legibly (the cap is"
            f" {MAX_SERIES} series) — {narrower}."
        )


def only_cut(cuts: Sequence[str], what: str) -> str:
    """The cut to draw when none was asked for: the only one persisted.

    Raises:
        ValueError: several cuts are persisted; the message names them.
    """
    if len(cuts) > 1:
        raise ValueError(
            f"{what} are persisted at {len(cuts)} cuts ({', '.join(cuts)}) — pass one"
            " with -p."
        )
    return cuts[0]


def new_figure(
    width: float, height: float, nrows: int = 1, ncols: int = 1, **kwargs: Any
) -> tuple[Figure, Any]:
    """A figure in the triage-pg style and its axes (``Figure.subplots`` semantics).

    Call it inside :func:`style`.
    """
    fig = Figure(figsize=(width, height), layout="constrained")
    return fig, fig.subplots(nrows, ncols, squeeze=False, **kwargs)


def pct_axis(ax: Axes, axis: str = "x") -> None:
    """Format ``axis`` as a percentage of 1."""
    fmt = PercentFormatter(1.0, decimals=0)
    (ax.xaxis if axis == "x" else ax.yaxis).set_major_formatter(fmt)


def log_ratio_axis(ax: Axes) -> None:
    """A log10 ratio axis: the 1× line and ticks read back as multiples."""
    ax.axvline(0, color=INK, linewidth=0.8)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{10**v:.2g}×"))


def no_grid(ax: Axes, axis: Literal["x", "y"]) -> None:
    """Drop the gridlines of one axis (a bar chart keeps the value axis's only)."""
    ax.grid(axis=axis, visible=False)


def as_date(value: date | str) -> date:
    """An ``as_of_date`` as the readers return it."""
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def nan(value: Optional[float]) -> float:
    """A value to draw: ``None`` (an undefined rate) becomes a gap."""
    return float("nan") if value is None else float(value)


def fmt_rate(value: Optional[float], digits: int = 2) -> str:
    """A rate for a label or a summary line; ``—`` when undefined."""
    return "—" if value is None else f"{value:.{digits}f}"


def span(values: Sequence[Optional[float]], digits: int = 2) -> str:
    """``lo–hi`` over the defined values (one value alone if they agree)."""
    defined = [v for v in values if v is not None]
    if not defined:
        return "—"
    lo, hi = min(defined), max(defined)
    if f"{lo:.{digits}f}" == f"{hi:.{digits}f}":
        return f"{lo:.{digits}f}"
    return f"{lo:.{digits}f}–{hi:.{digits}f}"
