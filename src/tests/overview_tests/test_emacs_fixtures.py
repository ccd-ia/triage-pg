"""The JSON Emacs reads, pinned: live CLI output must keep the shape of the committed fixtures.

``emacs/test/fixtures/*.json`` are what the ERT suite renders. This test runs every verb
``triage.el`` calls against a seeded database and compares the *shape* of the output with
the fixture: the keys, recursively, the JSON type of each value and the shape of a list's
first element. Values are not compared. ``null`` and an empty list match any type, because
a seeded value may legitimately be absent.

A rename fails here first. Regenerate with ``just emacs-fixtures``
(``TRIAGE_REGEN_EMACS_FIXTURES=1``), then run ``just emacs-test``: the ERT tests that still
read the old field fail there, so the drift is caught on both sides.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
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
)

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
        for diff in shape_differences(payload, json.loads(path.read_text())):
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
