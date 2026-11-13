"""``triage serve``: the dashboard from any project, with the CLI's database resolution."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from typer.testing import CliRunner

from triage.cli import app

runner = CliRunner()


@pytest.fixture
def uvicorn_calls(monkeypatch):
    import uvicorn

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    return calls


def test_serve_starts_the_dashboard_app(uvicorn_calls) -> None:
    result = runner.invoke(app, ["serve", "--port", "8123", "--reload"])
    assert result.exit_code == 0, result.output
    assert uvicorn_calls == [
        (
            ("triage.dashboard.app:app",),
            {"host": "127.0.0.1", "port": 8123, "reload": True},
        )
    ]


def test_serve_refuses_dbfile(uvicorn_calls, tmp_path) -> None:
    dbfile = tmp_path / "db.yaml"
    dbfile.write_text("host: h\nuser: u\ndb: d\npass: p\nport: 5432\n")
    result = runner.invoke(app, ["--dbfile", str(dbfile), "serve"])
    assert result.exit_code == 2
    assert "--dbfile is not passed to the dashboard" in " ".join(result.output.split())
    assert uvicorn_calls == []


def test_serve_without_the_extra_names_it(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # import raises ImportError
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "triage-pg[dashboard]" in " ".join(result.output.split())
