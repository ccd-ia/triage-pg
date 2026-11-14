"""The problem-type table as ``triage postmodel kinds --json`` prints it for triage.el.

triage.el greys out a menu entry when the model or Experiment at point has a problem type
the command refuses. It reads the table from this hidden verb, so the CLI keeps the only
copy. The tests pin what it prints, that every gated name is a real command, and that the
gate the baseline comparison runs reads the same entry the verb prints.
"""

from __future__ import annotations

import json
from typing import Any

import click
import pytest
import typer
from typer.testing import CliRunner

from tests.test_baseline_compare import EXP
from tests.test_baseline_compare import _seed as seed_baseline
from triage import problem_types
from triage.cli import app
from triage.diagnostics.baseline_compare import ComparisonError, compare_to_baseline
from triage.overview import CONTRACT

runner = CliRunner()


def _kinds() -> dict[str, Any]:
    result = runner.invoke(app, ["postmodel", "kinds", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_the_verb_prints_the_table_the_gates_read() -> None:
    payload = _kinds()
    assert payload["contract"] == CONTRACT
    assert payload["problem_types"] == list(problem_types.PROBLEM_TYPES)
    assert payload["commands"].keys() == problem_types.COMMANDS.keys()
    for name, entry in problem_types.COMMANDS.items():
        assert payload["commands"][name] == {
            "accepts": list(entry.accepts),
            "instead": dict(entry.instead),
        }


def test_the_error_tree_rule_of_each_type() -> None:
    classification = ["fp", "fn", "confusion", "both", "all"]
    assert _kinds()["error_tree"] == {
        "classification": {"kinds": classification, "horizon": "refused"},
        "regression_ranking": {
            "kinds": [*classification, "abs-error", "beats-baseline"],
            "horizon": "refused",
        },
        "regression": {"kinds": ["abs-error", "beats-baseline"], "horizon": "refused"},
        "survival": {"kinds": classification, "horizon": "required"},
    }


@pytest.mark.parametrize("problem_type", problem_types.PROBLEM_TYPES)
def test_the_error_tree_table_follows_the_refusal(problem_type: str) -> None:
    rule = problem_types.error_tree_table()[problem_type]
    horizon = 1.0 if rule["horizon"] == "required" else None
    for kind in (
        problem_types.CLASSIFICATION_TREE_KINDS + problem_types.REGRESSION_TREE_KINDS
    ):
        refused = problem_types.error_tree_refusal(7, problem_type, kind, horizon)
        assert (refused is None) == (kind.replace("_", "-") in rule["kinds"]), kind


def test_every_gated_name_is_a_command_of_the_cli() -> None:
    """``postmodel crosstabs --by abs-error`` names a command and the option it gates."""
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    for name in problem_types.COMMANDS:
        words = name.split()
        group = root.commands[words[0]]
        assert isinstance(group, click.Group), name
        command = group.commands[words[1]]
        options = {o for p in command.params for o in getattr(p, "opts", [])}
        for word in words[2:]:
            if word.startswith("--"):
                assert word in options, name


def test_the_verb_is_hidden_from_the_help() -> None:
    result = runner.invoke(app, ["postmodel", "--help"])
    assert result.exit_code == 0
    assert "kinds" not in result.stdout
    assert "residuals" in result.stdout


def test_the_baseline_gate_reads_the_table(db_pool_greenfield, monkeypatch) -> None:
    groups = seed_baseline(db_pool_greenfield)
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "update triage.experiments set problem_type = 'classification'"
            " where experiment_hash = %(h)s",
            {"h": EXP},
        )
    with pytest.raises(
        ComparisonError, match="reads regression and regression_ranking"
    ):
        compare_to_baseline(db_pool_greenfield, groups["dominant"])
    # the two figures go through the same comparison, so they share the entry
    entry = problem_types.COMMANDS["postmodel baseline"]
    assert problem_types.COMMANDS["plot segments"] is entry
    assert problem_types.COMMANDS["plot win-consistency"] is entry
    monkeypatch.setitem(
        problem_types.COMMANDS,
        "postmodel baseline",
        problem_types.CommandTypes(("classification", *entry.accepts)),
    )
    assert compare_to_baseline(db_pool_greenfield, groups["dominant"]).segments
