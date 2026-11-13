"""``triage experiment show TARGET``: identity, validation, the DB-free plan and the history."""

from __future__ import annotations

import json
import re
import shutil

import yaml

from tests.overview_tests.conftest import DIRTYDUCK_CONFIG
from triage.adapters.preflight import plan_experiment
from triage.adapters.run import experiment_hash_for


def _config(tmp_path):
    path = tmp_path / "inspections.yaml"
    shutil.copy(DIRTYDUCK_CONFIG, path)
    return path, yaml.full_load(path.read_text())


def test_file_not_run_yet(cli, tmp_path) -> None:
    path, config = _config(tmp_path)
    obj = cli.json("experiment", "show", path.name, "--json")

    assert obj["contract"] == 1
    assert obj["source"] == "file" and obj["path"] == path.name
    assert obj["experiment_hash"] == experiment_hash_for(config)
    assert obj["problem_type"] == "classification"
    assert obj["validation"]["valid"] is True
    assert obj["plan_error"] is None and obj["config_yaml"] is None
    assert obj["database"]["connected"] is True
    assert obj["history"] is None


def test_plan_is_the_planners_numbers(cli, tmp_path) -> None:
    path, config = _config(tmp_path)
    plan = cli.json("experiment", "show", path.name, "--json")["plan"]
    expected = plan_experiment(config)

    assert plan["n_splits"] == expected.n_splits
    assert plan["n_matrices"] == expected.n_matrices
    assert plan["n_model_groups"] == expected.n_model_groups
    assert plan["n_runs"] == expected.n_runs
    assert plan["n_models"] == expected.n_models
    assert plan["n_feature_columns"] == expected.n_feature_columns
    assert [s["label"] for s in plan["subsets"]] == [s.label for s in expected.subsets]


def test_plan_equals_analyze_config(cli, tmp_path) -> None:
    path, _ = _config(tmp_path)
    plan = cli.json("experiment", "show", path.name, "--json")["plan"]
    text = cli.run("analyze-config", path.name).stdout

    def row(label: str) -> str:
        match = re.search(rf"{label}\s+(\d+)", text)
        assert match, f"{label!r} not in analyze-config output:\n{text}"
        return match.group(1)

    assert int(row("Temporal splits")) == plan["n_splits"]
    assert int(row("Model groups")) == plan["n_model_groups"]
    assert int(row("Feature-group runs")) == plan["n_runs"]
    assert int(row("Models to be trained")) == plan["n_models"]
    assert int(row("Feature columns")) == plan["n_feature_columns"]


def test_file_that_has_run_shows_its_history(cli, tmp_path, db_pool_greenfield) -> None:
    path, config = _config(tmp_path)
    experiment_hash = experiment_hash_for(config)
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values (%(h)s, %(c)s::jsonb, 'classification')",
            {"h": experiment_hash, "c": json.dumps({"name": "x"})},
        )
        conn.execute(
            "insert into triage.runs (experiment_hash, profile, status)"
            " values (%(h)s, 'local', 'failed')",
            {"h": experiment_hash},
        )
    history = cli.json("experiment", "show", path.name, "--json")["history"]

    assert history["summary"]["n_runs"] == 1
    assert [r["state"] for r in history["runs"]] == ["failed"]
    assert history["model_groups"] == [] and history["model_group_metric"] is None


def test_hash_prefix_reads_the_stored_config(cli, seeded) -> None:
    obj = cli.json("experiment", "show", seeded.experiment_hash[:5], "--json")

    assert obj["source"] == "database" and obj["path"] is None
    assert obj["experiment_hash"] == seeded.experiment_hash
    assert obj["name"] == "Churn baseline"
    assert yaml.safe_load(obj["config_yaml"]) == {
        "cohort_name": "active",
        "label_name": "churn",
    }
    # the seed's stored config is not a real one: the error is shown, not raised
    assert obj["plan"] is None and obj["plan_error"]
    assert obj["validation"]["valid"] is False
    history = obj["history"]
    assert {r["run_id"] for r in history["runs"]} == {seeded.run_id, seeded.rerun_id}
    assert len(history["model_groups"]) == 3
    assert history["model_group_metric"]["metric"] in {"auc_roc", "average_precision"}
    assert history["leaderboard"]


def test_runs_and_top_limit_the_history(cli, seeded) -> None:
    history = cli.json(
        "experiment",
        "show",
        seeded.experiment_hash,
        "--json",
        "--runs",
        "1",
        "--top",
        "2",
    )["history"]
    assert len(history["runs"]) == 1 and len(history["leaderboard"]) == 2


def test_unknown_target_exits_2(cli) -> None:
    result = cli.run("experiment", "show", "nope-not-a-file", "--json")
    assert result.exit_code == 2
    assert "neither a config file nor the prefix" in " ".join(result.output.split())


def test_ambiguous_prefix_exits_2(cli, db_pool_greenfield) -> None:
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-dash-2', '{}'::jsonb, 'classification')"
        )
    result = cli.run("experiment", "show", "exp-d", "--json")
    assert result.exit_code == 2
    assert "Ambiguous experiment prefix" in " ".join(result.output.split())


def test_offline_file_target_still_plans(offline_cli, tmp_path) -> None:
    path, _ = _config(tmp_path)
    obj = offline_cli.json("experiment", "show", path.name, "--json")
    assert obj["database"]["connected"] is False
    assert obj["history"] is None and obj["plan"]["n_models"] > 0


def test_offline_hash_target_exits_2(offline_cli) -> None:
    result = offline_cli.run("experiment", "show", "abc123", "--json")
    assert result.exit_code == 2
    assert "without a reachable database" in " ".join(result.output.split())


def test_text_output_has_the_same_numbers(cli, seeded) -> None:
    out = cli.run("experiment", "show", seeded.experiment_hash).stdout
    assert seeded.experiment_hash in out
    assert "Cannot plan this config" in out
    assert "Model groups" in out and "Leaderboard" in out


def test_unparsable_file_exits_2(cli, tmp_path) -> None:
    (tmp_path / "broken.yaml").write_text("temporal_config: {a: [\n")
    result = cli.run("experiment", "show", "broken.yaml", "--json")
    assert result.exit_code == 2
    assert "cannot parse" in " ".join(result.output.split())
