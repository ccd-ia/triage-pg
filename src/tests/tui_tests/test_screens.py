"""The shell over the seeded DB, driven with Pilot: every tab renders what the views hold."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from lynkeus.testing import settle
from textual.widgets import DataTable, Input, Static

from triage.tui.app import build_app
from triage.tui.screens import AuditionScreen, ExperimentsScreen, LeaderboardScreen

NOW = datetime(2026, 9, 2, 10, 11)
SIZE = (120, 36)


@pytest.fixture
def app(db_url, seeded, tmp_path, monkeypatch):
    """The real shell over the seeded throwaway DB, frozen clock, no polling."""
    monkeypatch.chdir(tmp_path)
    for key in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGPORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", db_url)  # the R subprocess resolves it
    return build_app(
        db_url,
        poll_seconds=0,
        dashboard_url="http://dash.example",
        cwd=tmp_path,
        clock=lambda: NOW,
    )


async def test_standard_screens_render_the_seed(app, seeded) -> None:
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        status = app.screen_for("status")
        assert "connected" in str(status.query_one("#status-db", Static).render())
        assert "Churn baseline" in str(
            status.query_one("#status-runs", Static).render()
        )

        await pilot.press("2")
        await settle(pilot)
        runs = app.screen_for("runs")
        assert runs.query_one("#runs-table", DataTable).row_count == 2
        assert "models" in str(runs.query_one("#run-stages", Static).render())

        await pilot.press("3")
        await settle(pilot)
        tree = app.screen_for("data").query_one("#data-tree")
        labels = [str(node.label) for node in tree.root.children]
        assert any(label.startswith("triage") for label in labels)

        await pilot.press("4", "ctrl+r")
        await settle(pilot)
        query = app.screen_for("query")
        assert "rows" in str(query.query_one("#query-status", Static).render())

        await pilot.press("5")
        await settle(pilot)
        actions = app.screen_for("actions")
        assert actions.query_one("#actions-table", DataTable).row_count > 10


async def test_experiments_drills_into_runs(app, seeded) -> None:
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("6")
        await settle(pilot)
        experiments = app.screen_for("experiments")
        assert isinstance(experiments, ExperimentsScreen)
        assert experiments.query_one("#experiments-table", DataTable).row_count == 1
        assert experiments.selected is not None
        assert experiments.selected["experiment_hash"] == seeded.experiment_hash
        detail = str(experiments.query_one("#experiment-detail", Static).render())
        assert "Churn baseline" in detail and "base rate" in detail

        await pilot.press("enter")
        await settle(pilot)
        assert app.current_slug == "runs"
        runs = app.screen_for("runs")
        assert runs.query_one("#runs-filter", Input).value == "Churn baseline"
        assert runs.query_one("#runs-table", DataTable).row_count == 2


async def test_leaderboard_and_audition_follow_the_experiment(app, seeded) -> None:
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("7")
        await settle(pilot)
        leaderboard = app.screen_for("leaderboard")
        assert isinstance(leaderboard, LeaderboardScreen)
        assert leaderboard.context.experiment_hash == seeded.experiment_hash
        assert leaderboard.context.metric == "auc_roc"
        table = leaderboard.query_one("#leaderboard-table", DataTable)
        assert table.row_count == 3
        # mg2 has the best mean (0.81) on auc_roc — first row
        first = table.get_row_at(0)
        assert str(first[0]) == str(seeded.group_ids["mg2"])

        await pilot.press("m")
        await settle(pilot)
        assert leaderboard.context.metric == "average_precision"

        await pilot.press("8")
        await settle(pilot)
        audition = app.screen_for("audition")
        assert isinstance(audition, AuditionScreen)
        assert audition.context.metric == "average_precision"
        assert audition.query_one("#audition-table", DataTable).row_count == 3
        assert all(len(g["distances"]) == 3 for g in audition.groups)

        await pilot.press("4")
        await settle(pilot)
        editor_text = app.screen_for("query").query_one("#query-editor").text
        assert "triage.audition_distances" in editor_text


async def test_leaderboard_refresh_runs_the_cli(app, seeded) -> None:
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        await pilot.press("7")
        await settle(pilot)
        leaderboard = app.screen_for("leaderboard")
        assert isinstance(leaderboard, LeaderboardScreen)
        await pilot.press("R")
        await settle(pilot)
        assert leaderboard.process is not None
        assert leaderboard.process.wait(timeout=60) == 0
        await settle(pilot, ticks=10)
        assert "exit 0" in str(
            leaderboard.query_one("#leaderboard-note", Static).render()
        )


async def test_screenshots_of_every_tab(app, tmp_path: Path) -> None:
    """Every tab renders without an error toast; SVGs land in tmp for a human to look at."""
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot)
        for key in ("1", "2", "3", "4", "5", "6", "7", "8", "question_mark"):
            await pilot.press(key)
            await settle(pilot)
            assert not app.query("Toast"), f"error toast on tab {key}"
            app.save_screenshot(str(tmp_path / f"tab-{key}.svg"))
    assert (tmp_path / "tab-7.svg").exists()
