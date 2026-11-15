"""Which postmodel and plot commands read which problem types.

A diagnostic is defined for some problem types and meaningless for the rest: a residual
needs a continuous forecast, a confusion leaf needs a positive class. :data:`COMMANDS`
holds that table, one entry per command. :func:`require` reads a model's problem type from
the database (``triage.model_problem_type``, which reads a model with no run lineage as
classification) and refuses a type the command does not read, naming the command that
answers the same question for that type.

The table is data, so the CLI, the dashboard and an editor front end can read one copy:
:func:`kinds_table` is what the hidden ``triage postmodel kinds --json`` prints for
triage.el, which greys out a menu entry the model or Experiment at point would refuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from triage.util.db import DictRowPool

#: Every ``problem_type`` an Experiment can declare (ADR-0010).
PROBLEM_TYPES = ("classification", "regression_ranking", "regression", "survival")

#: The types with a continuous forecast and an observed value to subtract from it.
REGRESSION_TYPES = ("regression", "regression_ranking")

#: The types whose labeled rows split into TP, FP, FN and TN at a top-k cut: a binary
#: outcome, or a regression_ranking target read as positive when ``outcome > 0``.
CUT_CLASS_TYPES = ("classification", "regression_ranking")


@dataclass(frozen=True)
class CommandTypes:
    """The problem types one command reads, and what to run instead for the others.

    Attributes:
        accepts: the types the command reads.
        instead: for a type it refuses, the command that answers the nearest question.
    """

    accepts: tuple[str, ...]
    instead: Mapping[str, str] = field(default_factory=dict)


_FOR_CLASSIFIERS = "triage plot calibration MODEL or triage postmodel error-tree MODEL"
_FOR_SURVIVAL = "triage postmodel survival MODEL --horizon H"

#: For a model that is not survival, what answers the question a survival command asks.
_NOT_SURVIVAL = {
    "classification": "triage plot calibration MODEL",
    "regression": "triage postmodel residuals MODEL",
    "regression_ranking": "triage plot gains MODEL",
}

#: The baseline comparison reads the Experiment's problem type, not a model's: its gate
#: in diagnostics/baseline_compare.py reads this entry, and both of its figures go
#: through it.
_BASELINE_COMPARISON = CommandTypes(
    REGRESSION_TYPES,
    {
        "classification": "triage plot overlap GROUP_A GROUP_B",
        "survival": _FOR_SURVIVAL,
    },
)

#: Command → the problem types it reads.
COMMANDS: dict[str, CommandTypes] = {
    "postmodel residuals": CommandTypes(
        REGRESSION_TYPES,
        {"classification": _FOR_CLASSIFIERS, "survival": _FOR_SURVIVAL},
    ),
    "plot forecast-actual": CommandTypes(
        REGRESSION_TYPES,
        {"classification": "triage plot calibration MODEL", "survival": _FOR_SURVIVAL},
    ),
    "plot residuals": CommandTypes(
        REGRESSION_TYPES,
        {"classification": "triage plot calibration MODEL", "survival": _FOR_SURVIVAL},
    ),
    "plot bias": CommandTypes(
        REGRESSION_TYPES,
        {"classification": "triage plot curve-dates GROUP", "survival": _FOR_SURVIVAL},
    ),
    "postmodel crosstabs --by abs-error": CommandTypes(
        REGRESSION_TYPES,
        {
            "classification": "triage postmodel crosstabs MODEL -p 10_pct",
            "survival": "triage postmodel crosstabs MODEL -p 10_pct",
        },
    ),
    "plot forecast-time": CommandTypes(
        REGRESSION_TYPES,
        {"classification": "triage plot curve-dates GROUP", "survival": _FOR_SURVIVAL},
    ),
    # A ranking of a non-negative outcome: a binary one (recall@k) or a continuous one.
    "plot gains": CommandTypes(
        ("classification", *REGRESSION_TYPES),
        {"survival": _FOR_SURVIVAL},
    ),
    # The censoring-aware set (migration 0036): a survival label only.
    "postmodel survival": CommandTypes(("survival",), _NOT_SURVIVAL),
    "plot km": CommandTypes(("survival",), _NOT_SURVIVAL),
    "plot brier": CommandTypes(("survival",), _NOT_SURVIVAL),
    "plot survival-calibration": CommandTypes(
        ("survival",),
        {t: "triage plot calibration MODEL" for t in _NOT_SURVIVAL},
    ),
    # The bands and classes of a top-k cut (migration 0037): positive means outcome > 0.
    "postmodel near-miss": CommandTypes(
        CUT_CLASS_TYPES,
        {"regression": "triage plot gains MODEL", "survival": _FOR_SURVIVAL},
    ),
    "postmodel prototypes": CommandTypes(
        CUT_CLASS_TYPES,
        {
            "regression": "triage postmodel crosstabs MODEL --by abs-error",
            "survival": "triage postmodel error-tree MODEL --kind confusion --horizon H",
        },
    ),
    "postmodel baseline": _BASELINE_COMPARISON,
    "plot segments": _BASELINE_COMPARISON,
    "plot win-consistency": _BASELINE_COMPARISON,
}


class WrongProblemType(ValueError):
    """A command was asked about a model of a problem type it does not read."""


def model_problem_type(pool: DictRowPool, model_id: int) -> str:
    """The problem type a model was trained for, as the database records it.

    Raises:
        ValueError: no model has this id.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "select triage.model_problem_type(m.model_id)::text as problem_type"
            " from triage.models m where m.model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
    if row is None:
        raise ValueError(
            f"No model with id {model_id} — list a model group's models with:"
            " triage models <experiment> --group <id>"
        )
    return row["problem_type"]


def refusal(command: str, model_id: int, problem_type: str) -> str | None:
    """The message refusing ``problem_type`` for ``command``, or None when it is read."""
    entry = COMMANDS[command]
    if problem_type in entry.accepts:
        return None
    message = (
        f"triage {command} reads {' and '.join(entry.accepts)} models; model {model_id}"
        f" is {problem_type}."
    )
    instead = entry.instead.get(problem_type)
    if instead:
        message += f" For a {problem_type} model use {instead.replace('MODEL', str(model_id))}."
    return message


def require(pool: DictRowPool, command: str, model_id: int) -> str:
    """The model's problem type, when ``command`` reads it.

    Raises:
        WrongProblemType: the command does not read this type; the message names the
            command to run instead.
        ValueError: no model has this id.
    """
    problem_type = model_problem_type(pool, model_id)
    message = refusal(command, model_id, problem_type)
    if message is not None:
        raise WrongProblemType(message)
    return problem_type


#: The error-tree kinds a top-k cut over a binary outcome defines.
CLASSIFICATION_TREE_KINDS = ("fp", "fn", "confusion", "both", "all")

#: The error-tree kinds a continuous forecast defines (migration 0034).
REGRESSION_TREE_KINDS = ("abs_error", "beats_baseline")


def error_tree_refusal(
    model_id: int, problem_type: str, kind: str, horizon: float | None = None
) -> str | None:
    """Why ``postmodel error-tree --kind KIND [--horizon H]`` does not apply, or None.

    ``regression`` has no positive class, so it reads only the regression kinds.
    ``regression_ranking`` reads both families: its classification trees read a positive
    as ``outcome > 0``. ``classification`` reads only the classification kinds.
    ``survival`` reads them at a horizon only: ``--horizon H`` makes the label binary, and
    no other type reads ``--horizon``.
    """
    shown = kind.replace("_", "-")
    if horizon is not None and problem_type != "survival":
        return (
            f"model {model_id} is {problem_type}: --horizon binarizes a survival label"
            " and reads survival models only. Drop --horizon."
        )
    if problem_type == "regression" and kind in CLASSIFICATION_TREE_KINDS:
        return (
            f"model {model_id} is a regression model: --kind {shown} needs a positive"
            " class and a top-k cut. Use --kind abs-error (where the error is large) or"
            " --kind beats-baseline (where the model beats its baseline)."
        )
    if problem_type not in REGRESSION_TYPES and kind in REGRESSION_TREE_KINDS:
        return (
            f"model {model_id} is {problem_type}: --kind {shown} reads regression and"
            " regression_ranking models. Use --kind fp, fn, confusion, both or all."
        )
    if problem_type == "survival" and horizon is None:
        return (
            f"model {model_id} is a survival model: --kind {shown} needs a binary"
            " outcome. Add --horizon H (in the units of the label's duration): positive"
            " means an observed event at a duration ≤ H, and rows censored before H are"
            " dropped."
        )
    return None


def error_tree_table() -> dict[str, dict[str, Any]]:
    """The error-tree kinds each problem type reads, and its ``--horizon`` rule.

    Derived from :func:`error_tree_refusal`, so the table cannot drift from the gate.
    ``horizon`` is ``required`` when the type reads its kinds only at a horizon (survival),
    ``refused`` when it reads them only without one, and ``optional`` when it reads both.
    The kinds are spelled as ``--kind`` takes them (``abs-error``, not ``abs_error``).
    """
    kinds = CLASSIFICATION_TREE_KINDS + REGRESSION_TREE_KINDS
    table: dict[str, dict[str, Any]] = {}
    for problem_type in PROBLEM_TYPES:
        plain = [k for k in kinds if error_tree_refusal(0, problem_type, k) is None]
        at_horizon = [
            k for k in kinds if error_tree_refusal(0, problem_type, k, 1.0) is None
        ]
        if plain and at_horizon:
            horizon = "optional"
        elif at_horizon:
            horizon = "required"
        else:
            horizon = "refused"
        read = [k for k in kinds if k in plain or k in at_horizon]
        table[problem_type] = {
            "kinds": [k.replace("_", "-") for k in read],
            "horizon": horizon,
        }
    return table


#: The kinds of leaf an error tree persists (``triage.error_analysis.error_kind``).
LEAF_KINDS = ("fp", "fn", "confusion", *REGRESSION_TREE_KINDS)


def leaf_kinds(problem_type: str) -> tuple[str, ...]:
    """The kinds of leaf a model of ``problem_type`` can persist, in :data:`LEAF_KINDS`
    order. Derived from :func:`error_tree_refusal`, with or without a horizon."""
    return tuple(
        k
        for k in LEAF_KINDS
        if error_tree_refusal(0, problem_type, k) is None
        or error_tree_refusal(0, problem_type, k, 1.0) is None
    )


def group_problem_types(
    pool: DictRowPool, model_group_id: int, experiment_hash: str | None = None
) -> tuple[str, ...]:
    """The problem types a model group's models were trained for, in
    :data:`PROBLEM_TYPES` order.

    A group is shared by every Experiment whose grid fits the same estimator,
    hyperparameters and features, so it can span types. ``experiment_hash`` keeps the
    models of that Experiment. Empty when the group has no model (in it).
    """
    with pool.connection() as conn:
        found = {
            r["problem_type"]
            for r in conn.execute(
                "select distinct triage.model_problem_type(m.model_id)::text"
                "       as problem_type"
                " from triage.models as m"
                " left join triage.runs as r on r.run_id = m.run_id"
                " where m.model_group_id = %(g)s"
                "   and (%(exp)s::text is null or r.experiment_hash = %(exp)s)",
                {"g": model_group_id, "exp": experiment_hash},
            ).fetchall()
        }
    return tuple(t for t in PROBLEM_TYPES if t in found)


def recurrence_refusal(
    model_group_id: int, problem_types: tuple[str, ...], kind: str
) -> str | None:
    """Why a model group's error trees have no ``kind`` leaves, or None.

    A group of several types reads a kind any of them reads. A group with no model is
    not refused here: the reader answers it.
    """
    if not problem_types or any(kind in leaf_kinds(t) for t in problem_types):
        return None
    kinds = [k for k in LEAF_KINDS if any(k in leaf_kinds(t) for t in problem_types)]
    first = kinds[0]
    cut = " -p 'CUT|horizon=H'" if first == "fp" and "survival" in problem_types else ""
    return (
        f"model group {model_group_id} is {' and '.join(problem_types)}: its error"
        f" trees have {', '.join(k.replace('_', '-') for k in kinds)} leaves, not"
        f" {kind.replace('_', '-')}. Use triage postmodel error-recurrence"
        f" {model_group_id} --kind {first.replace('_', '-')}{cut}."
    )


def kinds_table() -> dict[str, Any]:
    """Which commands read which problem types, as ``triage postmodel kinds --json``
    prints it.

    Returns:
        ``problem_types``; ``commands``, each gated command (as :data:`COMMANDS` names it,
        an option included where the option is what is gated) with the types it
        ``accepts`` and, for a type it refuses, the command to run ``instead``; and
        ``error_tree`` from :func:`error_tree_table`. A command missing from
        ``commands`` reads every problem type.
    """
    return {
        "problem_types": list(PROBLEM_TYPES),
        "commands": {
            name: {"accepts": list(entry.accepts), "instead": dict(entry.instead)}
            for name, entry in COMMANDS.items()
        },
        "error_tree": error_tree_table(),
    }
