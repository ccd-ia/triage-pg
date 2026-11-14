"""The JSON Emacs reads, pinned: live CLI output must keep the shape of the committed fixtures.

``emacs/test/fixtures/*.json`` are what the ERT suite renders. This test runs every verb
``triage.el`` calls against a seeded database and compares the *shape* of the output with
the fixture: the keys, recursively, the JSON type of each value and the shape of a list's
first element. Values are not compared. ``null`` and an empty list match any type, because
a seeded value may legitimately be absent.

A rename fails here first. Regenerate with ``just emacs-fixtures``
(``TRIAGE_REGEN_EMACS_FIXTURES=1``), then run ``just emacs-test``: the ERT tests that still
read the old field fail there, so the drift is caught on both sides.

Two fixtures are compared by value, not shape: the problem-type table of ``triage
postmodel kinds --json`` and the command tree (each plot, postmodel and check subcommand
with its options). The ERT suite checks the menus against them, so a new plot kind or a
newly gated command fails here until the fixtures are regenerated, and then fails in ERT
until triage.el has an entry for it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import click
import pytest
import typer
import yaml
from typer.testing import CliRunner

from tests.dashboard_tests.conftest import SeededExperiment, _seed_full_experiment
from tests.overview_tests.conftest import (
    DIRTYDUCK_CONFIG,
    REPO_ROOT,
    UNREACHABLE_URL,
    Cli,
    _cli,
)
from triage.cli import app

FIXTURES = REPO_ROOT / "emacs" / "test" / "fixtures"
REGENERATE = os.environ.get("TRIAGE_REGEN_EMACS_FIXTURES") == "1"
CONFIG_NAME = "inspections.yaml"

#: Every fixture the ERT suite reads; the test fails if one is missing or unused.
NAMES = (
    "status-full",
    "status-full-offline",
    "experiment-show-file",
    "experiment-show-hash",
    "runs-list",
    "runs-show",
    "model-show",
    "models",
    "leaderboard",
    "query",
    "actions-list",
    "leakage-report",
    "postmodel-kinds",
    "cli-commands",
    "model-show-regression",
    "residuals",
    "baseline",
    "structure",
    "structure-tree",
    "importance",
    "sensitivity",
    "compare-rows",
    "survival",
    "near-miss",
    "prototypes",
    "metrics-report",
    "history-report",
)

#: The fixtures compared by value: the ERT suite reads their values, not only their keys.
VALUE_PINNED = ("postmodel-kinds", "cli-commands")

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
# Only timezone-aware timestamps: those are now()-generated. Seeded dates stay as they are.
_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)"
)


# ------------------------------------------------------------------ shape


def _type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def shape_differences(live: Any, fixture: Any, path: str = "$") -> list[str]:
    """Where ``live`` and ``fixture`` differ in shape, as ``path: what`` lines."""
    if live is None or fixture is None:
        return []
    if isinstance(live, list) and isinstance(fixture, list):
        if not live or not fixture:
            return []
        return shape_differences(live[0], fixture[0], f"{path}[0]")
    if isinstance(live, dict) and isinstance(fixture, dict):
        out = [
            f"{path}.{k}: in the live output, not in the fixture"
            for k in live.keys() - fixture.keys()
        ]
        out += [
            f"{path}.{k}: in the fixture, not in the live output"
            for k in fixture.keys() - live.keys()
        ]
        for key in sorted(live.keys() & fixture.keys()):
            out += shape_differences(live[key], fixture[key], f"{path}.{key}")
        return sorted(out)
    if _type(live) != _type(fixture):
        return [f"{path}: live {_type(live)}, fixture {_type(fixture)}"]
    return []


# ------------------------------------------------------------------ normalization


def _normalize(payload: Any, tmp_path: Path) -> Any:
    """Stable ids, timestamps and paths, so a regeneration diffs only what changed."""
    text = json.dumps(payload, indent=2, sort_keys=False)
    text = text.replace(str(tmp_path), "/project")
    uuids: dict[str, str] = {}
    text = _UUID.sub(
        lambda m: uuids.setdefault(
            m.group(0), f"00000000-0000-4000-8000-{len(uuids) + 1:012d}"
        ),
        text,
    )
    stamps: dict[str, str] = {}
    text = _TIMESTAMP.sub(
        lambda m: stamps.setdefault(
            m.group(0), f"2026-01-01T00:{len(stamps):02d}:00+00:00"
        ),
        text,
    )
    out = json.loads(text)
    if isinstance(out, dict) and "elapsed_s" in out:
        out["elapsed_s"] = 1.0
    return out


def _check(live: dict[str, Any], tmp_path: Path) -> None:
    """Compare (or, regenerating, write) each named payload; fail with every difference."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for name, payload in live.items():
        path = FIXTURES / f"{name}.json"
        if REGENERATE:
            path.write_text(json.dumps(_normalize(payload, tmp_path), indent=2) + "\n")
            continue
        if not path.exists():
            failures.append(f"{path.name}: missing")
            continue
        fixture = json.loads(path.read_text())
        if name in VALUE_PINNED:
            if _normalize(payload, tmp_path) != fixture:
                failures.append(f"{path.name}: the live values differ from the fixture")
            continue
        for diff in shape_differences(payload, fixture):
            failures.append(f"{path.name} {diff}")
    if failures:
        pytest.fail(
            "The CLI's JSON no longer has the shape of emacs/test/fixtures:\n  "
            + "\n  ".join(failures)
            + "\nIf the change is intended: run `just emacs-fixtures`, then `just"
            " emacs-test` and update the Elisp that reads the changed keys. A rename or"
            " removal also bumps triage.overview.CONTRACT.",
            pytrace=False,
        )


# ------------------------------------------------------------------ the verbs


def _use_dirtyduck_config(pool, seeded: SeededExperiment) -> None:
    """Give the seeded Experiment a real config, so its object has a plan and a history."""
    config = yaml.full_load(DIRTYDUCK_CONFIG.read_text())
    with pool.connection() as conn:
        conn.execute(
            "update triage.experiments set config = %(c)s::jsonb where experiment_hash = %(h)s",
            {"c": json.dumps(config, default=str), "h": seeded.experiment_hash},
        )


def _score_the_shown_model(pool, seeded: SeededExperiment) -> None:
    """Score the model ``model show`` prints at its evaluated date, so the fixture pins the
    shape of a calibration decile and not an empty list."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.predictions"
            " (model_id, entity_id, as_of_date, split_kind, score)"
            " select %(shown)s, entity_id, as_of_date, split_kind, score"
            " from triage.predictions where model_id = %(first)s",
            {"shown": seeded.latest_model["mg1"], "first": seeded.all_models["mg1"][0]},
        )


def _cli_payloads(cli: Cli, seeded: SeededExperiment) -> dict[str, Any]:
    run_id, model_id = seeded.run_id, seeded.latest_model["mg1"]
    exp = seeded.experiment_hash
    return {
        "status-full": cli.json("status", "--json", "--full"),
        "experiment-show-file": cli.json("experiment", "show", CONFIG_NAME, "--json"),
        "experiment-show-hash": cli.json("experiment", "show", exp, "--json"),
        "runs-list": cli.json("runs", "list", "--json"),
        "runs-show": cli.json("runs", "show", run_id, "--json"),
        "model-show": cli.json("model", "show", str(model_id), "--json"),
        "models": cli.json("models", exp, "--json"),
        "leaderboard": cli.json("leaderboard", exp, "--json", "--limit", "10"),
        "query": cli.json(
            "query",
            "select model_group_id, model_type from triage.model_groups order by 1",
            "--json",
        ),
        "actions-list": cli.json("actions", "list", "--json"),
    }


def test_fixture_names_are_the_ones_the_elisp_reads() -> None:
    if REGENERATE:
        pytest.skip("regenerating")
    on_disk = {p.stem for p in FIXTURES.glob("*.json")}
    assert on_disk == set(NAMES)


def test_seeded_verbs_keep_the_fixture_shapes(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
) -> None:
    seeded = _seed_full_experiment(db_pool_greenfield)
    _use_dirtyduck_config(db_pool_greenfield, seeded)
    _score_the_shown_model(db_pool_greenfield, seeded)
    shutil.copy(DIRTYDUCK_CONFIG, tmp_path / CONFIG_NAME)
    live = _cli_payloads(_cli(monkeypatch, tmp_path, db_url), seeded)
    live["status-full-offline"] = _cli(monkeypatch, tmp_path, UNREACHABLE_URL).json(
        "status", "--json", "--full"
    )
    assert (
        live["experiment-show-hash"]["history"] and live["experiment-show-hash"]["plan"]
    )
    assert live["experiment-show-file"]["history"] is None
    _check(live, tmp_path)


def test_leakage_report_keeps_the_fixture_shape(
    db_url, db_pool, tmp_path, monkeypatch
) -> None:
    """The report ``check leakage --json PATH`` writes; a planted label leak fills findings."""
    from tests.test_leakage_check import LEAKY_LABELS, _config, _seed

    _seed(db_pool)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "experiment.yaml"
    config_path.write_text(yaml.safe_dump(_config(labels=LEAKY_LABELS)))
    report_path = tmp_path / "report.json"
    result = CliRunner().invoke(
        app,
        ["check", "leakage", str(config_path), "--json", str(report_path)],
        env={"DATABASE_URL": db_url},
    )
    assert result.exit_code == 1, result.output
    report = json.loads(report_path.read_text())
    assert report["findings"]
    _check({"leakage-report": report}, tmp_path)


# ------------------------------------------------------------------ the type table


def _command_tree() -> dict[str, Any]:
    """Each subcommand of plot, postmodel and check with its arguments and options, and
    audition's options: what triage.el's menus pass. Hidden commands are left out, as
    ``--help`` leaves them out."""
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)

    def describe(command: click.Command) -> dict[str, Any]:
        options = [p for p in command.params if isinstance(p, click.Option)]
        return {
            "arguments": [
                p.name for p in command.params if isinstance(p, click.Argument)
            ],
            "options": [o for p in options for o in p.opts if o != "--help"],
            "required": [max(p.opts, key=len) for p in options if p.required],
        }

    tree: dict[str, Any] = {}
    for name in ("plot", "postmodel", "check"):
        group = root.commands[name]
        assert isinstance(group, click.Group)
        tree[name] = {
            sub: describe(command)
            for sub, command in group.commands.items()
            if not command.hidden
        }
    tree["audition"] = describe(root.commands["audition"])
    return tree


def test_the_type_table_and_the_command_tree_keep_their_values(tmp_path) -> None:
    result = CliRunner().invoke(app, ["postmodel", "kinds", "--json"])
    assert result.exit_code == 0, result.output
    _check(
        {"postmodel-kinds": json.loads(result.stdout), "cli-commands": _command_tree()},
        tmp_path,
    )


# ------------------------------------------------------------------ the postmodel views


def _evaluate(pool, experiment: str, timespan: str) -> None:
    from triage.component.catwalk.in_pg_evaluation import evaluate_in_db

    with pool.connection() as conn:
        pairs = conn.execute(
            "select distinct p.model_id, p.as_of_date from triage.predictions p"
            " join triage.models m using (model_id) join triage.runs r using (run_id)"
            " where r.experiment_hash = %(h)s and p.split_kind = 'test'",
            {"h": experiment},
        ).fetchall()
    for p in pairs:
        evaluate_in_db(pool, p["model_id"], p["as_of_date"], timespan)


def _first_model(pool, model_group_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "select min(model_id) as m from triage.models where model_group_id = %(g)s",
            {"g": model_group_id},
        ).fetchone()["m"]


def _seed_postmodel(pool, data: Path) -> dict[str, Any]:
    """One Experiment per view, from the phase tests' seeders: the ids the verbs read."""
    from tests.test_baseline_compare import _seed as seed_baseline
    from tests.test_common_rows import EXP as COMMON_EXP
    from tests.test_common_rows import _seed as seed_common_rows
    from tests.test_near_miss_prototypes import seed_model
    from tests.test_structure import _ridge_design, _seed_groups, _store
    from tests.test_survival_diagnostics import seed_survival

    ids: dict[str, Any] = {"classifier": seed_model(pool, data, tag="clf")}
    _evaluate(pool, "exp-clf", "6 months")
    groups = seed_baseline(pool)
    _evaluate(pool, "exp-baseline", "1 month")
    ids["regression_group"] = groups["dominant"]
    ids["regression"] = _first_model(pool, groups["dominant"])
    ridge, x_train, x_test = _ridge_design()
    ids["ridge"] = _store(pool, data, ridge, x_train, x_test, "ridge")
    from sklearn.tree import DecisionTreeRegressor

    tree = DecisionTreeRegressor(max_depth=3).fit(x_train, x_train[:, 0])
    ids["tree"] = _store(pool, data, tree, x_train, x_test, "tree")
    ids["survival"] = seed_survival(pool, data)["model_id"]
    seed_common_rows(pool)
    ids["common"] = COMMON_EXP
    ids["sensitivity_group"] = _seed_groups(
        pool, [{"alpha": a, "fit_intercept": True} for a in (0.1, 1.0, 10.0)]
    )[0]
    return ids


def test_postmodel_verbs_keep_the_fixture_shapes(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    ids = _seed_postmodel(db_pool_greenfield, data)
    cli = _cli(monkeypatch, tmp_path, db_url)
    report = tmp_path / "metrics.json"
    audit = cli.run(
        "check", "metrics", "--experiment", "exp-clf", "--json", str(report)
    )
    assert audit.exit_code == 0, audit.output
    live = {
        "model-show-regression": cli.json(
            "model", "show", str(ids["regression"]), "--json"
        ),
        "residuals": cli.json(
            "postmodel", "residuals", str(ids["regression"]), "--json"
        ),
        "baseline": cli.json(
            "postmodel", "baseline", str(ids["regression_group"]), "--json"
        ),
        "structure": cli.json("postmodel", "structure", str(ids["ridge"]), "--json"),
        "structure-tree": cli.json(
            "postmodel", "structure", str(ids["tree"]), "--json"
        ),
        "importance": cli.json(
            "postmodel", "importance", str(ids["sensitivity_group"]), "--json"
        ),
        "sensitivity": cli.json(
            "postmodel", "sensitivity", "exp-sens", "--param", "alpha", "--json"
        ),
        "compare-rows": cli.json("postmodel", "compare-rows", ids["common"], "--json"),
        "survival": cli.json(
            "postmodel", "survival", str(ids["survival"]), "--horizon", "30", "--json"
        ),
        "near-miss": cli.json(
            "postmodel", "near-miss", str(ids["classifier"]), "--json"
        ),
        "prototypes": cli.json(
            "postmodel", "prototypes", str(ids["classifier"]), "-n", "2", "--json"
        ),
        "metrics-report": json.loads(report.read_text()),
    }
    # the views draw these parts: the fixture must not pin an empty list for them
    assert live["residuals"]["dates"] and live["residuals"]["largest_errors"]
    assert live["baseline"]["chance"] and live["baseline"]["segments"]
    assert live["structure"]["mean_shift"]
    assert live["structure-tree"]["leaf_support"]
    assert live["importance"]["models"] and live["importance"]["stability"]
    assert live["survival"]["dates"] and live["prototypes"] and live["near-miss"]
    assert live["model-show-regression"]["calibration"]
    assert live["model-show-regression"]["card"]["problem_type"] == "regression"
    _check(live, tmp_path)


def test_history_report_keeps_the_fixture_shape(
    db_url, db_pool, tmp_path, monkeypatch
) -> None:
    """The report ``check history --json PATH`` writes; one series has gaps."""
    from tests.test_history_check import SPARSE, _config, _run, seed_events

    seed_events(db_pool)
    result, report = _run(tmp_path, monkeypatch, db_url, _config(SPARSE))
    assert result.exit_code == 1, result.output
    assert report["worst_entities"]
    _check({"history-report": report}, tmp_path)
