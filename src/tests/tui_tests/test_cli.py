"""The headless verbs print what the screens show; ``--json`` is machine-clean."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from triage.cli import app


@pytest.fixture
def invoke(db_url, seeded, tmp_path, monkeypatch):
    """Run the CLI against the seeded DB via DATABASE_URL, from a yaml-free cwd."""
    monkeypatch.chdir(tmp_path)
    for key in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGPORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", db_url)
    runner = CliRunner()

    def run(*args: str):
        result = runner.invoke(app, list(args), catch_exceptions=False)
        assert result.exit_code == 0, result.output
        return result.output

    return run


def _json_tail(output: str):
    """The JSON payload after the loguru 'Using database' line(s)."""
    start = min(i for i in (output.find("{"), output.find("[")) if i >= 0)
    return json.loads(output[start:])


def test_status_json(invoke, seeded) -> None:
    payload = _json_tail(invoke("status", "--json"))

    assert payload["database"]["connected"] is True
    assert {r["run_id"] for r in payload["last_runs"]} == {
        seeded.rerun_id,
        seeded.run_id,
    }
    assert payload["pending"][0]["level"] == "ok"


def test_runs_list_show_tail_json(invoke, seeded) -> None:
    listed = _json_tail(invoke("runs", "list", "--json", "--limit", "1"))
    assert len(listed) == 1 and listed[0]["run_id"] in {seeded.rerun_id, seeded.run_id}

    shown = _json_tail(invoke("runs", "show", seeded.run_id[:8], "--json"))
    assert shown["run"]["run_id"] == seeded.run_id
    assert {s["name"] for s in shown["stages"]} >= {"cohort", "models", "evaluations"}

    tail = invoke("runs", "tail", seeded.run_id, "--json")
    events = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert events[-1]["subject"] == "run"


def test_query_json_and_error(invoke) -> None:
    rows = _json_tail(
        invoke("query", "select count(*) as n from triage.runs", "--json")
    )
    assert rows == [{"n": 2}]

    out = invoke("query", "select * from triage.nope")
    assert "does not exist" in out


def test_actions_list_and_run(invoke) -> None:
    actions = _json_tail(invoke("actions", "list", "--json"))
    assert any(a["name"] == "triage gc" and a["destructive"] for a in actions)

    out = invoke("actions", "run", "triage", "--", "--version")
    assert "triage-pg" in out


def test_runs_status_still_needs_a_region(invoke, monkeypatch) -> None:
    """The pre-existing cloud verb keeps working alongside list/show/tail.

    The region has to be cleared: a developer whose ``.envrc`` exports
    ``AWS_REGION`` gets past this check and on into botocore's credential
    chain, where a local ``~/.aws/login`` profile raises about a missing
    ``botocore[crt]`` instead. CI has neither, which is why the test only
    ever failed on a laptop.
    """
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    runner = CliRunner()
    result = runner.invoke(app, ["runs", "status"])
    assert result.exit_code != 0
    assert "AWS_REGION" in result.output
