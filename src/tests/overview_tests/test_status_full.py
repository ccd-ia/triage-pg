"""``triage status --json --full``: one call with everything the Emacs status buffer draws."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from tests.overview_tests.conftest import DIRTYDUCK_CONFIG
from triage.adapters.run import experiment_hash_for
from triage.overview import discover_configs

LYNKEUS_KEYS = {
    "project",
    "database",
    "last_runs",
    "pending",
    "extra",
    "gauges",
    "series",
}
FULL_KEYS = (
    {"contract", "triage_version", "project_root"}
    | LYNKEUS_KEYS
    | {
        "experiments",
        "configs",
        "sources",
        "relations",
        "warnings",
    }
)


def _tree(root: Path) -> None:
    """A project with one good config, one broken config and YAML that is not a config."""
    (root / "experiments").mkdir()
    shutil.copy(DIRTYDUCK_CONFIG, root / "experiments" / "inspections.yaml")
    (root / "experiments" / "broken.yaml").write_text(
        "temporal_config: {feature_start_time: 2014\nlabel_config: [\n"
    )
    (root / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")
    (root / "notes.yaml").write_text("{{ templated }}: [\n")  # broken, but not a config
    (root / "README.txt").write_text("temporal_config label_config")
    for skipped in (".venv", "node_modules", ".hidden"):
        (root / skipped).mkdir()
        shutil.copy(DIRTYDUCK_CONFIG, root / skipped / "inside.yaml")


def test_full_needs_json(cli) -> None:
    result = cli.run("status", "--full")
    assert result.exit_code == 2
    assert "--full is JSON-only; add --json" in " ".join(result.output.split())


def test_plain_status_json_is_unchanged(cli) -> None:
    assert set(cli.json("status", "--json")) == LYNKEUS_KEYS


def test_full_adds_the_project_around_the_lynkeus_status(cli, seeded) -> None:
    payload = cli.json("status", "--json", "--full")

    assert set(payload) == FULL_KEYS
    assert payload["contract"] == 1
    assert payload["database"]["connected"] is True
    [experiment] = payload["experiments"]
    assert experiment["experiment_hash"] == seeded.experiment_hash
    assert experiment["name"] == "Churn baseline"
    assert experiment["n_runs"] == 2
    assert experiment["last_status"] == "completed"
    assert experiment["archived_at"] is None
    assert payload["sources"], "the seed pins sources"
    kinds = {r["name"]: r for r in payload["relations"]}
    assert kinds["predictions"]["kind"] == "partitioned"
    assert kinds["leaderboard"]["kind"] == "matview"
    assert kinds["experiment_summary"]["kind"] == "view"
    assert kinds["experiment_summary"]["est_rows"] is None
    assert isinstance(kinds["runs"]["est_rows"], int)
    # partitions are folded into their parent, not listed one by one
    assert not any(name.startswith("predictions_") for name in kinds)


def test_experiments_agree_with_experiment_summary(cli, db_pool_greenfield) -> None:
    payload = cli.json("status", "--json", "--full")
    with db_pool_greenfield.connection() as conn:
        view = conn.execute(
            "select experiment_hash, name, n_runs, last_status::text as last_status"
            " from triage.experiment_summary"
        ).fetchall()
    assert [
        {k: e[k] for k in ("experiment_hash", "name", "n_runs", "last_status")}
        for e in payload["experiments"]
    ] == view


def test_configs_are_found_hashed_and_filtered(cli, tmp_path) -> None:
    _tree(tmp_path)
    payload = cli.json("status", "--json", "--full")

    by_path = {c["path"]: c for c in payload["configs"]}
    assert set(by_path) == {"experiments/broken.yaml", "experiments/inspections.yaml"}
    good = by_path["experiments/inspections.yaml"]
    expected = experiment_hash_for(yaml.full_load(DIRTYDUCK_CONFIG.read_text()))
    assert good["experiment_hash"] == expected
    assert good["error"] is None
    assert good["problem_type"] == "classification"
    broken = by_path["experiments/broken.yaml"]
    assert broken["experiment_hash"] is None and broken["error"]


def test_configs_glob_narrows_the_search(cli, tmp_path) -> None:
    _tree(tmp_path)
    payload = cli.json(
        "status", "--json", "--full", "--configs", "experiments/inspections.yaml"
    )
    assert [c["path"] for c in payload["configs"]] == ["experiments/inspections.yaml"]


def test_discovery_stops_at_the_limit_and_says_so(tmp_path) -> None:
    for i in range(3):
        shutil.copy(DIRTYDUCK_CONFIG, tmp_path / f"c{i}.yaml")
    entries, warnings = discover_configs(tmp_path, limit=2)
    assert [e.path for e in entries] == ["c0.yaml", "c1.yaml"]
    assert len(warnings) == 1 and "--configs" in warnings[0]


def test_unreachable_database_still_lists_configs(offline_cli, tmp_path) -> None:
    _tree(tmp_path)
    payload = offline_cli.json("status", "--json", "--full")

    assert payload["database"]["connected"] is False
    assert payload["database"]["detail"]
    assert payload["experiments"] == payload["sources"] == payload["relations"] == []
    assert len(payload["configs"]) == 2


def test_empty_project_has_no_experiments(
    db_url, db_pool_greenfield, tmp_path, monkeypatch
):
    from tests.overview_tests.conftest import _cli

    payload = _cli(monkeypatch, tmp_path, db_url).json("status", "--json", "--full")
    assert payload["experiments"] == [] and payload["configs"] == []
    assert payload["database"]["connected"] is True
