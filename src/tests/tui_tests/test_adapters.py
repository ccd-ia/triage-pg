"""The three adapters are queries over the seeded schema — nothing stored, nothing guessed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from lynkeus import Action, ActionSource, RunState

from triage.cli import app as cli_app
from triage.tui.adapters import (
    SAVED_QUERIES,
    TriageActions,
    TriageRuns,
    TriageStatus,
    project_name,
)

# ------------------------------------------------------------------ status


def test_status_is_derived_from_the_schema(source, seeded) -> None:
    status = TriageStatus(source, "test-project").status()

    assert status.database.connected
    assert status.database.detail.startswith("pg ")
    assert status.project == "test-project"
    assert {r.run_id for r in status.last_runs} == {seeded.rerun_id, seeded.run_id}
    assert {r.state for r in status.last_runs} == {RunState.SUCCEEDED}
    assert status.last_runs[0].name == "Churn baseline"
    assert status.last_runs[0].detail == "9 models"
    gauges = {g.name: g.value for g in status.gauges}
    assert gauges["runs"] == 2
    assert gauges["models"] == 9
    assert gauges["model groups"] == 3
    assert gauges["experiments"] == 1
    assert "tables" in status.extra["size"]
    assert status.extra["schema"] != "unknown"
    assert "built" in status.extra["artifacts"]
    runs_per_day = status.series[0]
    assert runs_per_day.name == "runs per day"
    assert len(runs_per_day.values) == 14
    assert runs_per_day.values[-1] == 2.0
    assert not runs_per_day.empty
    assert runs_per_day.empty_note == "none in 14 d"
    # both runs completed, leaderboard refreshed, every experiment has a run
    assert [p.level for p in status.pending] == ["ok"]
    json.dumps(status.to_json())


def test_a_fortnight_without_a_run_says_so_instead_of_drawing_a_flat_line(
    source, seeded
) -> None:
    with source.connect() as conn:
        conn.execute("update triage.runs set started_at = now() - interval '30 days'")

    runs_per_day = TriageStatus(source, "test-project").status().series[0]

    assert runs_per_day.values == [0.0] * 14
    assert runs_per_day.empty
    assert runs_per_day.summary() == "none in 14 d"


def test_status_flags_a_stale_run_and_an_orphaned_build(source, seeded) -> None:
    with source.connect() as conn:
        conn.execute(
            "update triage.runs set status = 'started',"
            " started_at = now() - interval '7 hours', finished_at = null"
            " where run_id = %(r)s",
            {"r": seeded.rerun_id},
        )
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config,"
            " status, built_by_run) values ('stuck', 'stuck', 'matrix', '{}'::jsonb,"
            " 'building', %(r)s)",
            {"r": seeded.run_id},
        )
        conn.commit()

    pending = {p.name: p for p in TriageStatus(source, "p").status().pending}

    assert pending["runs"].level == "error"
    assert "7 h" not in pending["runs"].detail  # threshold, not the age
    assert "after 6 h" in pending["runs"].detail
    assert pending["artifacts"].level == "warn"
    assert "1 stuck" in pending["artifacts"].detail


def test_status_when_the_database_is_down() -> None:
    from lynkeus import PgSource

    status = TriageStatus(
        PgSource(dsn="postgresql://nobody@127.0.0.1:1/none"), "p"
    ).status()

    assert status.database.connected is False
    assert status.last_runs == [] and status.pending == []


# -------------------------------------------------------------------- runs


def test_runs_list_show_and_prefix_resolution(source, seeded) -> None:
    runs = TriageRuns(source, dashboard_url="http://dash.example/")

    listed = runs.list(10)
    assert {r.run_id for r in listed} == {seeded.rerun_id, seeded.run_id}
    assert listed[0].state is RunState.SUCCEEDED

    detail = runs.show(seeded.run_id[:8])
    assert detail.run.run_id == seeded.run_id
    stages = {s.name: s for s in detail.stages}
    assert (stages["cohort"].done, stages["cohort"].total) == (1, 1)
    assert (stages["labels"].done, stages["labels"].total) == (1, 1)
    assert (stages["models"].done, stages["models"].total) == (9, 9)
    assert stages["evaluations"].done == 9
    assert stages["predictions"].done == 1  # only the first model has predictions
    assert detail.meta["git"] == "abc1234"
    assert detail.meta["url"] == f"http://dash.example/runs/{seeded.run_id}"
    assert detail.meta["experiment"] == seeded.experiment_hash[:8]

    rerun = runs.show(seeded.rerun_id)
    assert "cached" in {s.name: s for s in rerun.stages}["models"].note


def test_runs_show_rejects_an_ambiguous_prefix(source, seeded) -> None:
    with pytest.raises(LookupError, match="ambiguous|no run"):
        TriageRuns(source).show("")
    with pytest.raises(LookupError, match="no run"):
        TriageRuns(source).show("zzzzzzzz")


def test_events_replays_a_finished_run_and_stops(source, seeded) -> None:
    raw = list(TriageRuns(source).events(seeded.run_id))
    events = [e for e in raw if e is not None]

    assert len(events) == len(raw), "a replay never idles, so it never yields None"
    assert events[-1].subject == "run" and events[-1].kind == "completed"
    kinds = {(e.subject, e.kind) for e in events[:-1]}
    assert ("cohort", "built") in kinds and ("model", "built") in kinds


def test_events_listen_forwards_this_runs_notifies_only(source, seeded) -> None:
    with source.connect() as conn:
        conn.execute(
            "update triage.runs set status = 'started', finished_at = null"
            " where run_id = %(r)s",
            {"r": seeded.run_id},
        )
        conn.commit()
    stream = TriageRuns(source).events(seeded.run_id)
    # prime the generator so LISTEN is active before the notifies fire
    first = next(stream)
    assert first is None or first.kind == "fallback"
    with source.connect(autocommit=True) as conn:
        for run_id, kind, status in (
            (seeded.rerun_id, "matrix", "built"),  # another run: ignored
            (seeded.run_id, "matrix", "built"),
            (seeded.run_id, "run", "completed"),
        ):
            conn.execute(
                "select pg_notify('run_progress', %(p)s)",
                {"p": json.dumps({"run_id": run_id, "kind": kind, "status": status})},
            )
    got = [e for e in stream if e is not None]

    assert [(e.subject, e.kind) for e in got] == [
        ("matrix", "built"),
        ("run", "completed"),
    ]


def test_cancel_is_explicit_about_not_existing(source) -> None:
    with pytest.raises(NotImplementedError, match="triage run"):
        TriageRuns(source).cancel("x")


# ----------------------------------------------------------------- actions
# The justfile and typer parsing is lynkeus's and is tested there
# (lynkeus/tests/test_actions.py). What is asserted here is this project's
# palette: which verbs prompt for what, and which are confirmed before they run.


@pytest.fixture
def verbs(tmp_path: Path) -> dict[str, Action]:
    """The CLI half of the palette, listed from a directory without a justfile."""
    return {a.name: a for a in TriageActions(cwd=tmp_path, cli_app=cli_app).list()}


def test_cli_args_are_the_required_arguments_typer_prints_in_its_usage(
    verbs: dict[str, Action],
) -> None:
    # `triage run` with no config exits 2 with usage — the shell prompts instead
    assert verbs["triage run"].args == "CONFIG"
    assert verbs["triage predictlist"].args == "MODEL_ID AS_OF_DATE"
    assert verbs["triage postmodel compare"].args == "MODEL_A MODEL_B"
    assert verbs["triage project drop"].args == "SLUG"
    # options and defaulted arguments are not prompted for: the verb runs bare
    assert verbs["triage gc"].args == ""
    assert verbs["triage status"].args == ""
    assert verbs["triage runs list"].args == ""


def test_cli_actions_cover_the_typer_tree_and_mark_the_destructive_verbs(
    verbs: dict[str, Action],
) -> None:
    for name in ("triage run", "triage leaderboard", "triage tui", "triage status"):
        assert name in verbs, name
    for name in ("triage runs list", "triage runs show", "triage runs tail"):
        assert name in verbs, name
    for name in ("triage query", "triage actions list", "triage actions run"):
        assert name in verbs, name
    destructive = {n for n, a in verbs.items() if a.destructive}
    assert destructive == {
        "triage gc",
        "triage archive",
        "triage db downgrade",
        "triage project drop",
    }
    assert verbs["triage analyze-config"].description


def test_actions_list_and_run_in_a_directory_without_a_justfile(tmp_path: Path) -> None:
    adapter = TriageActions(cwd=tmp_path, cli_app=cli_app)
    names = {a.name for a in adapter.list()}
    assert "triage --version" not in names
    assert "triage status" in names
    assert not any(n.startswith("just ") for n in names)

    process = adapter.run("triage", ["--version"])
    out = process.stdout.read() if process.stdout else ""
    assert process.wait() == 0
    assert "triage-pg" in out


def test_run_falls_back_to_python_m_when_the_console_script_is_off_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout run from source has no ``triage`` on PATH; the shell still runs it.

    The base class then starts ``python -m triage.cli``, which works only because
    ``triage.cli`` guards ``__main__``: without the guard the fallback imports the
    module, runs nothing and exits 0 — indistinguishable from success.
    """
    monkeypatch.setattr(shutil, "which", lambda cmd, *args, **kwargs: None)
    process = TriageActions(cwd=tmp_path, cli_app=cli_app).run("triage", ["--version"])
    out = process.stdout.read() if process.stdout else ""
    assert process.wait() == 0
    assert "triage-pg" in out


@pytest.mark.skipif(shutil.which("just") is None, reason="just is not installed")
def test_actions_list_reads_the_repo_justfile() -> None:
    root = Path(__file__).resolve().parents[3]
    actions = {a.name: a for a in TriageActions(cwd=root).list()}
    assert "just tui" in actions and "just test" in actions
    assert actions["just test"].source is ActionSource.JUST
    # lynkeus's word rule confirms the `clean` and `rebuild` recipes; `test` runs bare
    assert actions["just tutorial-clean"].destructive
    assert actions["just tutorial-rebuild"].destructive
    assert not actions["just test"].destructive


def test_saved_queries_run_against_the_seeded_schema(source, seeded) -> None:
    for name, sql in SAVED_QUERIES.items():
        result = source.query(sql)
        assert not result.error, f"{name}: {result.error}"


def test_project_name_is_the_database_segment() -> None:
    assert (
        project_name("postgresql+psycopg://u:p@h:5/proj_db?sslmode=require")
        == "proj_db"
    )
