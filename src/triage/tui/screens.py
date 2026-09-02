"""triage-pg's own screens for the lynkeus shell: Experiments, Leaderboard, Audition.

Each one is a query over a view the dashboard already reads (ADR-0012):
``experiment_summary``, the ``leaderboard`` matview and
``audition_distances``. Sparklines only; anything richer opens the dashboard.
"""

from __future__ import annotations

from lynkeus import PgSource
from lynkeus.screens import ShellScreen


def project_screens(source: PgSource, dashboard_url: str) -> list[ShellScreen]:
    """The screens ``triage tui`` adds after the standard five (tabs 6+)."""
    return []
