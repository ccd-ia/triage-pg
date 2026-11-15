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


def test_the_leaf_kinds_of_each_type() -> None:
    """The kinds the dashboard's error panels offer (frontend/src/api/errorKinds.ts)."""
    cut = ("fp", "fn", "confusion")
    regression = ("abs_error", "beats_baseline")
    assert {t: problem_types.leaf_kinds(t) for t in problem_types.PROBLEM_TYPES} == {
        "classification": cut,
        "regression_ranking": (*cut, *regression),
        "regression": regression,
        "survival": cut,
    }
    # a group of several types reads the kinds of any of them
    assert problem_types.recurrence_refusal(7, ("regression",), "fp") is not None
    assert (
        problem_types.recurrence_refusal(7, ("classification", "regression"), "fp")
        is None
    )
    assert problem_types.recurrence_refusal(7, (), "fp") is None


def test_the_recurrence_kinds_are_the_leaf_kinds() -> None:
    """triage.el offers a group's recurrence kinds from this part of the table."""
    assert _kinds()["error_recurrence"] == {
        t: {"kinds": [k.replace("_", "-") for k in problem_types.leaf_kinds(t)]}
        for t in problem_types.PROBLEM_TYPES
    }


@pytest.mark.parametrize(
    ("command", "types", "kind", "named"),
    [
        ("postmodel error-recurrence", ("regression",), "fp", "--kind abs-error."),
        ("plot recurrence", ("regression",), "fn", "--kind abs-error."),
        ("postmodel error-recurrence", ("classification",), "abs_error", "--kind fp."),
        # plot recurrence takes no default cut for the classification kinds
        ("plot recurrence", ("classification",), "abs_error", "--kind fp -p CUT."),
        (
            "plot recurrence",
            ("survival",),
            "abs_error",
            "--kind fp -p 'CUT|horizon=H'.",
        ),
    ],
)
def test_the_recurrence_refusal_names_the_command_asked(
    command: str, types: tuple[str, ...], kind: str, named: str
) -> None:
    message = problem_types.recurrence_refusal(7, types, kind, command)
    assert message is not None
    assert message.endswith(f"Use triage {command} 7 {named}"), message


def test_the_curve_across_dates_reads_a_top_k_cut() -> None:
    entry = problem_types.COMMANDS["plot curve-dates"]
    assert entry.accepts == problem_types.CUT_CLASS_TYPES
    assert set(entry.instead) == {"regression", "survival"}
    assert (
        problem_types.group_refusal("plot curve-dates", 7, ("classification",)) is None
    )
    # a group of several types is read when any of them is; an empty one by the reader
    assert (
        problem_types.group_refusal(
            "plot curve-dates", 7, ("regression_ranking", "regression")
        )
        is None
    )
    assert problem_types.group_refusal("plot curve-dates", 7, ()) is None
    message = problem_types.group_refusal("plot curve-dates", 7, ("regression",))
    assert message is not None
    assert message.startswith(
        "triage plot curve-dates reads classification and regression_ranking model"
        " groups; model group 7 is regression."
    )
    assert "triage plot bias MODEL" in message and "--group 7" in message
    survival = problem_types.group_refusal("plot curve-dates", 7, ("survival",))
    assert survival is not None and "triage plot km MODEL" in survival


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


#: The postmodel, plot and model commands no problem type gates, and why each reads
#: every type. A new command is gated in triage.problem_types (COMMANDS, or the kind of
#: leaf it reads) or listed here with its reason: the walk below fails until it is one.
READS_EVERY_TYPE = {
    "postmodel crosstabs": (
        "the top k against the rest is a ranking of the scores; --by abs-error and"
        " --against near-miss are gated in COMMANDS"
    ),
    "postmodel compare": "two models' top-k lists and their rank correlation: a ranking",
    "postmodel compare-rows": "an Experiment's groups on the rows all of them scored",
    "postmodel structure": "the fitted estimator; it reads the type itself",
    "postmodel importance": "a group's persisted feature importances",
    "postmodel sensitivity": "an Experiment's metric by one hyperparameter",
    "postmodel kinds": "the table itself: it reads no model",
    "plot crosstabs": "the rows postmodel crosstabs persisted, at whichever cut",
    "plot crosstabs-dates": "the same rows over a group's test dates",
    "plot overlap": "two groups' top-k lists and their rank correlation: a ranking",
    "plot scores": "the score histogram, which reads the type itself",
    "plot importance": "a group's persisted feature importances",
    "model show": "the model card, which reads the type itself",
}

#: The CLI groups whose commands read a model, a model group or an Experiment's models.
_DIAGNOSTIC_GROUPS = ("postmodel", "plot", "model")


def _diagnostic_commands() -> list[str]:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    names: list[str] = []
    for group_name in _DIAGNOSTIC_GROUPS:
        group = root.commands[group_name]
        assert isinstance(group, click.Group), group_name
        names += [f"{group_name} {sub}" for sub in group.commands]
    return names


def _kind_gated() -> set[str]:
    """The commands gated by the kind of error-tree leaf they read, not in COMMANDS."""
    return {
        "postmodel error-tree",  # error_tree_refusal
        *problem_types.RECURRENCE_COMMANDS,
        *problem_types.LEAF_COMMANDS,
    }


def test_every_diagnostic_command_is_gated_or_reads_every_type() -> None:
    """The guard against an ungated command: each one is in COMMANDS, gates the kind of
    leaf it reads, or is listed in READS_EVERY_TYPE with its reason. A command with a
    gated option (``postmodel crosstabs --by abs-error``) reads every type without it,
    so it is listed too."""
    gated = {name for name in problem_types.COMMANDS if " --" not in name}
    gated |= _kind_gated()
    commands = _diagnostic_commands()
    assert len(commands) > 30
    unclassified = [c for c in commands if c not in gated and c not in READS_EVERY_TYPE]
    assert unclassified == [], (
        "gate these by problem type (triage.problem_types.COMMANDS, or a kind table) or"
        f" list them in READS_EVERY_TYPE with why they read every type: {unclassified}"
    )


def test_the_allow_list_names_ungated_commands_only() -> None:
    commands = set(_diagnostic_commands())
    assert set(READS_EVERY_TYPE) <= commands, set(READS_EVERY_TYPE) - commands
    assert _kind_gated() <= commands, _kind_gated() - commands
    whole = {name for name in problem_types.COMMANDS if " --" not in name}
    assert not set(READS_EVERY_TYPE) & (whole | _kind_gated())
    assert not whole & _kind_gated()


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
