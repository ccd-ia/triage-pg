"""``triage tui``: the lynkeus shell with triage-pg's adapters and screens.

The CLI resolves the project database exactly as every other verb does
(``--dbfile`` › ``database.yaml`` › ``DATABASE_URL`` › ``PG*``) and hands the
URL here; nothing in this module touches the environment itself.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from lynkeus.app import ShellApp

from triage import __version__
from triage.tui.adapters import (
    SAVED_QUERIES,
    TriageActions,
    TriageRuns,
    TriageStatus,
    project_name,
    source_for,
)

DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8000"

HELP_EXTRA = (
    "[$primary]6[/] Experiments — one row per prediction problem (ADR-0022); "
    "[$primary]enter[/] drills into its runs\n"
    "[$primary]7[/] Leaderboard — the triage.leaderboard matview; "
    "[$primary]R[/] refreshes it\n"
    "[$primary]8[/] Audition — distance-from-best per model group "
    "(triage.audition_distances)\n"
    "Anything richer than a sparkline opens in the dashboard "
    "([$primary]o[/] on a run)."
)


def state_dir(project: str) -> Path:
    """Where the Query screen keeps the user's own saved queries and CSV exports."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "triage" / "tui" / (project or "default")


def build_app(
    db_url: str,
    *,
    poll_seconds: float = 5.0,
    dashboard_url: str | None = None,
    cwd: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    with_project_screens: bool = True,
) -> ShellApp:
    """Assemble the shell over the resolved project database URL."""
    source = source_for(db_url)
    project = project_name(db_url)
    dashboard = (
        dashboard_url or os.environ.get("TRIAGE_DASHBOARD_URL") or DEFAULT_DASHBOARD_URL
    )
    screens = []
    if with_project_screens:
        from triage.tui.screens import project_screens

        screens = project_screens(source, dashboard)
    return ShellApp(
        project="triage-pg",
        subtitle=f"project {project}" if project else "",
        status_adapter=TriageStatus(source, project or "triage-pg"),
        runs_adapter=TriageRuns(source, dashboard),
        actions_adapter=TriageActions(cwd),
        source=source,
        project_screens=screens,
        saved_queries=SAVED_QUERIES,
        version=f"v{__version__}",
        poll_seconds=poll_seconds,
        clock=clock,
        state_dir=state_dir(project),
        help_extra=HELP_EXTRA,
    )
