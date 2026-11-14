"""Figures of a project's diagnostics, drawn from its database: what ``triage plot`` writes.

Each function takes a connection pool and ids, reads its rows through
:mod:`triage.diagnostics.readers` (the query layer the dashboard reads too, so a figure
and a panel cannot disagree) and returns a :class:`PlotResult`: the figure, the rows it
drew, and a one-line summary. ``result.save(path)`` writes ``.svg``, ``.png`` or
``.pdf``, by extension. Nothing here runs SQL of its own, and nothing needs the
dashboard extra.

    from triage.plots import curve
    from triage.util.db import connection_pool

    pool = connection_pool(url)
    curve(pool, 285, subsets=["restaurant", "school"]).save("curve-285.svg")

Errors: :class:`NothingToDraw` (a ``LookupError``) when the database holds none of the
rows a figure needs, naming the command that creates them; ``ValueError`` (including
:class:`~triage.diagnostics.readers.NotFoundError`) for an unknown id or Subset, an
unsupported extension, or more than :data:`MAX_SERIES` series.
"""

from triage.plots.crosstabs import crosstabs, crosstabs_dates
from triage.plots.curves import curve, curve_dates
from triage.plots.distributions import calibration, scores
from triage.plots.errors import error_rules, leaves, recurrence
from triage.plots.overlap import overlap
from triage.plots.style import (
    FORMATS,
    MAX_SERIES,
    NothingToDraw,
    PlotResult,
    output_format,
)

__all__ = [
    "FORMATS",
    "MAX_SERIES",
    "NothingToDraw",
    "PlotResult",
    "calibration",
    "crosstabs",
    "crosstabs_dates",
    "curve",
    "curve_dates",
    "error_rules",
    "leaves",
    "output_format",
    "overlap",
    "recurrence",
    "scores",
]
