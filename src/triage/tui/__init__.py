"""The triage-pg terminal cockpit, built on the lynkeus shell.

``triage tui`` opens it; ``triage status`` / ``triage runs list|show|tail`` /
``triage query`` / ``triage actions list|run`` print the same data headlessly
(``--json`` for agents). The adapters live in :mod:`triage.tui.adapters`, the
project screens in :mod:`triage.tui.screens`, the assembly in
:mod:`triage.tui.app`.
"""

from __future__ import annotations

from triage.tui.adapters import (
    SAVED_QUERIES,
    TriageActions,
    TriageRuns,
    TriageStatus,
    project_name,
    source_for,
)

__all__ = [
    "SAVED_QUERIES",
    "TriageActions",
    "TriageRuns",
    "TriageStatus",
    "build_app",
    "project_name",
    "source_for",
]


def build_app(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201 — lazy: Textual import
    """See :func:`triage.tui.app.build_app`."""
    from triage.tui.app import build_app as _build

    return _build(*args, **kwargs)
