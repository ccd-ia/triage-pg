"""Fixtures for the editor-facing verbs: the dashboard's seeded experiment plus a CLI runner.

The seed is the one the TUI and dashboard tests use (two runs sharing experiment
``exp-dash``, three model groups, evaluations, predictions, a refreshed leaderboard), so
``status --full`` and ``experiment show`` are checked against the shape those surfaces
render.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from tests.dashboard_tests.conftest import SeededExperiment, _seed_full_experiment
from triage.cli import app

REPO_ROOT = Path(__file__).resolve().parents[3]
DIRTYDUCK_CONFIG = REPO_ROOT / "example" / "dirtyduck" / "experiment.yaml"

#: A closed port: connecting fails at once, which is the "database down" state.
UNREACHABLE_URL = "postgresql://nobody@127.0.0.1:1/nowhere"


@dataclass
class Cli:
    """``run(*args)`` → the click Result; ``json(*args)`` → parsed stdout, exit 0 asserted."""

    run: Callable[..., Result]

    def json(self, *args: str):
        result = self.run(*args)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)


def _cli(monkeypatch, cwd: Path, url: str) -> Cli:
    monkeypatch.chdir(cwd)
    for key in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGPORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", url)
    runner = CliRunner()
    return Cli(lambda *args: runner.invoke(app, list(args), catch_exceptions=False))


@pytest.fixture
def seeded(db_pool_greenfield) -> SeededExperiment:
    return _seed_full_experiment(db_pool_greenfield)


@pytest.fixture
def cli(db_url, seeded, tmp_path, monkeypatch) -> Cli:
    """The CLI against the seeded DB, run from an empty project directory."""
    return _cli(monkeypatch, tmp_path, db_url)


@pytest.fixture
def offline_cli(tmp_path, monkeypatch) -> Cli:
    """The CLI with DATABASE_URL pointing at a closed port."""
    return _cli(monkeypatch, tmp_path, UNREACHABLE_URL)
