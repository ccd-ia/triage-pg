"""Which postmodel and plot commands read which problem types.

A diagnostic is defined for some problem types and meaningless for the rest: a residual
needs a continuous forecast, a confusion leaf needs a positive class. :data:`COMMANDS`
holds that table, one entry per command. :func:`require` reads a model's problem type from
the database (``triage.model_problem_type``, which reads a model with no run lineage as
classification) and refuses a type the command does not read, naming the command that
answers the same question for that type. :func:`require_group` does the same for a model
group's command, over the types of the group's models.

The commands that read persisted error-tree leaves gate the kind of leaf instead
(:func:`error_tree_refusal`, :data:`RECURRENCE_COMMANDS`, :data:`LEAF_COMMANDS`, all
derived from :func:`leaf_kinds`). Every other postmodel, plot and model command reads
every type; the test suite walks the CLI and fails on a command that is neither gated
here nor listed, with its reason, as reading every type.

The table is data, so the CLI, the dashboard and an editor front end can read one copy:
:func:`kinds_table` is what the hidden ``triage postmodel kinds --json`` prints for
triage.el, which greys out a menu entry the model or Experiment at point would refuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

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

#: Precision and recall at each cut of one test date: a positive class split at the cut.
#: A regression model's labeled rows are all positive (outcome > 0), so its precision
#: reads 1, and a survival label has no outcome to count. The plot and the printed
#: points share the entry.
_THRESHOLD_CURVE = CommandTypes(
    CUT_CLASS_TYPES,
    {"regression": "triage plot gains MODEL", "survival": "triage plot km MODEL"},
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
    # A model group's precision at each cut of each test date: a regression group's
    # labeled rows are all positive (outcome > 0), so its precision reads a flat 1. The
    # gate reads the group's types (group_refusal); MODEL is one of the group's models.
    "plot curve-dates": CommandTypes(
        CUT_CLASS_TYPES,
        {
            "regression": "triage plot bias MODEL or triage plot forecast-time MODEL",
            "survival": "triage plot km MODEL",
        },
    ),
    "plot curve": _THRESHOLD_CURVE,
    "model curve": _THRESHOLD_CURVE,
    # The near-miss band of postmodel near-miss, which reads the same cut types.
    "postmodel crosstabs --against near-miss": CommandTypes(
        CUT_CLASS_TYPES,
        {
            "regression": "triage postmodel crosstabs MODEL --by abs-error",
            "survival": "triage postmodel crosstabs MODEL -p 10_pct",
        },
    ),
    # A score bucket's mean score against what its rows realized: a survival label has
    # no outcome to average; its calibration is predicted S(H) against Kaplan-Meier.
    "plot calibration": CommandTypes(
        ("classification", *REGRESSION_TYPES),
        {"survival": "triage plot survival-calibration MODEL"},
    ),
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


def group_refusal(
    command: str, model_group_id: int, problem_types: tuple[str, ...]
) -> str | None:
    """The message refusing a model group for ``command``, or None when it is read.

    ``problem_types`` are the group's (:func:`group_problem_types`). A group of several
    types is read when any of them is. A group with no model is not refused here: the
    reader answers it.
    """
    entry = COMMANDS[command]
    if not problem_types or any(t in entry.accepts for t in problem_types):
        return None
    message = (
        f"triage {command} reads {' and '.join(entry.accepts)} model groups; model"
        f" group {model_group_id} is {' and '.join(problem_types)}."
    )
    for problem_type in problem_types:
        instead = entry.instead.get(problem_type)
        if instead:
            message += f" For a {problem_type} model group use {instead}"
            if "MODEL" in instead:
                message += (
                    " on one of its models (list them with: triage models <experiment>"
                    f" --group {model_group_id})"
                )
            return message + "."
    return message


def require_group(
    pool: DictRowPool, command: str, model_group_id: int
) -> tuple[str, ...]:
    """The model group's problem types, when ``command`` reads one of them.

    Raises:
        WrongProblemType: the command reads none of them; the message names the command
            to run instead.
    """
    problem_types = group_problem_types(pool, model_group_id)
    message = group_refusal(command, model_group_id, problem_types)
    if message is not None:
        raise WrongProblemType(message)
    return problem_types


def _spelled(kinds: Sequence[str]) -> str:
    """Kinds of leaf as ``--kind`` takes them, comma-separated (``abs-error``)."""
    return ", ".join(k.replace("_", "-") for k in kinds)


#: The commands that read one kind of a model group's persisted leaves, and whether a
#: classification kind needs its cut passed (``postmodel error-recurrence`` defaults to
#: 100_abs; ``plot recurrence`` takes no default).
RECURRENCE_COMMANDS: dict[str, bool] = {
    "postmodel error-recurrence": False,
    "plot recurrence": True,
}


def recurrence_refusal(
    model_group_id: int,
    problem_types: tuple[str, ...],
    kind: str,
    command: str = "postmodel error-recurrence",
) -> str | None:
    """Why a model group's error trees have no ``kind`` leaves, or None.

    A group of several types reads a kind any of them reads. A group with no model is
    not refused here: the reader answers it. The message names ``command``, one of
    :data:`RECURRENCE_COMMANDS`, with the kind to read instead.
    """
    needs_cut = RECURRENCE_COMMANDS[command]
    if not problem_types or any(kind in leaf_kinds(t) for t in problem_types):
        return None
    kinds = [k for k in LEAF_KINDS if any(k in leaf_kinds(t) for t in problem_types)]
    first = kinds[0]
    cut = ""
    if first not in REGRESSION_TREE_KINDS:
        if "survival" in problem_types:
            cut = " -p 'CUT|horizon=H'"
        elif needs_cut:
            cut = " -p CUT"
    return (
        f"model group {model_group_id} is {' and '.join(problem_types)}: its error"
        f" trees have {_spelled(kinds)} leaves, not"
        f" {kind.replace('_', '-')}. Use triage {command}"
        f" {model_group_id} --kind {first.replace('_', '-')}{cut}."
    )


#: The commands that read one model's persisted leaves of one ``--kind``
#: (:func:`leaf_refusal`), and the figure whose cut picks the kinds of a group's leaves
#: (:func:`leaves_refusal`). With :data:`RECURRENCE_COMMANDS` and ``postmodel
#: error-tree`` (:func:`error_tree_refusal`) they are the commands gated by the kind of
#: leaf they read rather than by :data:`COMMANDS`.
LEAF_COMMANDS = ("postmodel error-rules", "plot error-rules", "plot leaves")


def leaf_refusal(
    model_id: int, problem_type: str, kind: str, command: str = "postmodel error-rules"
) -> str | None:
    """Why a model's error trees have no ``kind`` leaves, or None.

    ``command`` is the one asked (``postmodel error-rules`` or ``plot error-rules``); the
    message names it with the first kind the type has.
    """
    kinds = leaf_kinds(problem_type)
    if kind in kinds:
        return None
    return (
        f"model {model_id} is {problem_type}: its error trees have"
        f" {_spelled(kinds)} leaves, not"
        f" {kind.replace('_', '-')}. Use triage {command} {model_id} --kind"
        f" {kinds[0].replace('_', '-')}."
    )


def require_leaf_kind(
    pool: DictRowPool, model_id: int, kind: str, command: str = "postmodel error-rules"
) -> str:
    """The model's problem type, when its error trees can have ``kind`` leaves.

    Raises:
        WrongProblemType: the type has no ``kind`` leaves; the message names the kind
            to read instead.
        ValueError: no model has this id.
    """
    problem_type = model_problem_type(pool, model_id)
    message = leaf_refusal(model_id, problem_type, kind, command)
    if message is not None:
        raise WrongProblemType(message)
    return problem_type


def leaves_refusal(
    model_group_id: int, problem_types: tuple[str, ...], parameter: str
) -> str | None:
    """Why ``plot leaves GROUP [-p CUT]`` finds no leaves the group's types have, or None.

    Without a cut (``parameter == ''``) the figure reads abs_error leaves, which have
    none; with one, the fp and confusion leaves fitted at it. A group of several types is
    read when any of them has the kinds. A group with no model is not refused here.
    """
    kind = "abs_error" if parameter == "" else "fp"
    if not problem_types or any(kind in leaf_kinds(t) for t in problem_types):
        return None
    kinds = [k for k in LEAF_KINDS if any(k in leaf_kinds(t) for t in problem_types)]
    if kind == "fp":
        use = f"triage plot leaves {model_group_id} with no -p (abs-error has no cut)"
    elif "survival" in problem_types:
        use = f"triage plot leaves {model_group_id} -p 'CUT|horizon=H'"
    else:
        use = f"triage plot leaves {model_group_id} -p CUT"
    read = "fp and confusion" if kind == "fp" else "abs-error"
    return (
        f"model group {model_group_id} is {' and '.join(problem_types)}: its error"
        f" trees have {_spelled(kinds)} leaves, not"
        f" {read}. Use {use}."
    )


def require_leaves(
    pool: DictRowPool, model_group_id: int, parameter: str
) -> tuple[str, ...]:
    """The group's problem types, when ``plot leaves`` at ``parameter`` reads them.

    Raises:
        WrongProblemType: none of the group's types has the kinds the cut reads.
    """
    problem_types = group_problem_types(pool, model_group_id)
    message = leaves_refusal(model_group_id, problem_types, parameter)
    if message is not None:
        raise WrongProblemType(message)
    return problem_types


def recurrence_kind(
    pool: DictRowPool,
    model_group_id: int,
    kind: str | None,
    command: str = "postmodel error-recurrence",
) -> str:
    """The kind of leaf ``command`` reads for a model group: ``kind`` when the group's
    problem type has it; with no ``kind``, the first kind the type has (fp, or
    abs_error for a regression group), as the dashboard's recurrence route reads it.

    Raises:
        WrongProblemType: none of the group's types has ``kind`` leaves; the message
            names the kind to read instead.
    """
    problem_types = group_problem_types(pool, model_group_id)
    if kind is None:
        return leaf_kinds(problem_types[0])[0] if problem_types else "fp"
    message = recurrence_refusal(model_group_id, problem_types, kind, command)
    if message is not None:
        raise WrongProblemType(message)
    return kind


def recurrence_table() -> dict[str, dict[str, list[str]]]:
    """The kinds of leaf the recurrence commands read for each problem type, spelled as
    ``--kind`` takes them: :func:`leaf_kinds`, which the gate reads."""
    return {
        t: {"kinds": [k.replace("_", "-") for k in leaf_kinds(t)]}
        for t in PROBLEM_TYPES
    }


def kinds_table() -> dict[str, Any]:
    """Which commands read which problem types, as ``triage postmodel kinds --json``
    prints it.

    Returns:
        ``problem_types``; ``commands``, each gated command (as :data:`COMMANDS` names it,
        an option included where the option is what is gated) with the types it
        ``accepts`` and, for a type it refuses, the command to run ``instead``;
        ``error_tree`` from :func:`error_tree_table`; and ``error_recurrence`` from
        :func:`recurrence_table`, the kinds of leaf ``postmodel error-recurrence`` and
        ``plot recurrence`` read per type, which the :data:`LEAF_COMMANDS` read too. A
        command missing from ``commands`` reads every problem type, but for the kind of
        leaf it is asked for.
    """
    return {
        "problem_types": list(PROBLEM_TYPES),
        "commands": {
            name: {"accepts": list(entry.accepts), "instead": dict(entry.instead)}
            for name, entry in COMMANDS.items()
        },
        "error_tree": error_tree_table(),
        "error_recurrence": recurrence_table(),
    }
