"""The contract of the two problem blocks: ``cohort_config`` and ``label_config`` (#21).

Both blocks define the prediction problem (ADR-0022), so both enter identity — the experiment
hash, and the cohort and labels derivations. A key the pipeline does not read is therefore not
harmless: it moves those hashes and does nothing else. A key ported from DSSG triage, or a
misspelled one, is rejected here instead of being carried silently.

The checks are written once, as ``(path, message)`` pairs, so the two consumers say the same
thing: :func:`validate_cohort_config` / :func:`validate_label_config` raise before anything is
built (``run_experiment``, ``triage run``, ``triage analyze-config``), and
:func:`triage.adapters.run.validate_experiment_config` reports the same pairs as path-addressed
errors for the dashboard's dry run.

``include_missing_labels_in_train_as`` is DSSG triage's key, with DSSG's semantics: absent, the
unlabeled rows of a train matrix are not fitted on; ``false`` fills them with 0, ``true`` with 1.
Train matrices only — test and production matrices keep the NULL, so evaluation never counts an
unlabeled row as a negative.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "COHORT_CONFIG_KEYS",
    "LABEL_CONFIG_KEYS",
    "MISSING_LABELS_KEY",
    "cohort_config_errors",
    "label_config_errors",
    "labels_identity_config",
    "missing_label_fill",
    "validate_cohort_config",
    "validate_label_config",
]

MISSING_LABELS_KEY = "include_missing_labels_in_train_as"

#: Every key the pipeline reads from ``cohort_config``. ``name``/``description`` are cosmetic
#: (``triage analyze-config`` prints them); ``forward_query`` is the production cohort (ADR-0032).
COHORT_CONFIG_KEYS = frozenset({"name", "description", "query", "forward_query"})

#: Every key the pipeline reads from ``label_config``.
LABEL_CONFIG_KEYS = frozenset({"name", "description", "query", MISSING_LABELS_KEY})

# A 0/1 fill only means something for a binary target: it would corrupt a continuous outcome,
# and a survival label has no ``outcome`` column to fill.
_FILLABLE_PROBLEM_TYPES = ("classification",)


def _query_of(block: Mapping[str, Any]) -> str | None:
    query = block.get("query")
    return query if isinstance(query, str) and query else None


def _unknown_key_errors(
    block: Mapping[str, Any], name: str, accepted: frozenset[str]
) -> list[tuple[str, str]]:
    return [
        (
            f"{name}.{key}",
            f"{name}: unknown key {key!r} — accepted keys are"
            + f" {', '.join(sorted(accepted))}",
        )
        for key in sorted(str(k) for k in block if k not in accepted)
    ]


def cohort_config_errors(cohort_config: object) -> list[tuple[str, str]]:
    """Every problem with ``cohort_config``, as ``(path, message)`` pairs; empty when valid."""
    if not isinstance(cohort_config, Mapping):
        return [("cohort_config", "cohort_config must be a mapping with a 'query'")]
    errors = _unknown_key_errors(cohort_config, "cohort_config", COHORT_CONFIG_KEYS)
    query = _query_of(cohort_config)
    if query is None:
        errors.append(("cohort_config.query", "cohort_config needs a 'query'"))
    elif "{as_of_date}" not in query:
        errors.append(
            (
                "cohort_config.query",
                "the cohort query must contain the {as_of_date} placeholder",
            )
        )
    # The production cohort a forward score runs over (ADR-0032, #10). Optional, and
    # identity-neutral (run._problem_identity). Held to the same placeholder rule as the
    # training query, because build_cohort renders both the same way.
    forward_query = cohort_config.get("forward_query")
    if forward_query is not None:
        if not isinstance(forward_query, str) or not forward_query.strip():
            errors.append(
                (
                    "cohort_config.forward_query",
                    "forward_query must be a non-empty SQL string returning entity_id",
                )
            )
        elif "{as_of_date}" not in forward_query:
            errors.append(
                (
                    "cohort_config.forward_query",
                    "the forward cohort query must contain the {as_of_date} placeholder",
                )
            )
    return errors


def label_config_errors(
    label_config: object, problem_type: object = None
) -> list[tuple[str, str]]:
    """Every problem with ``label_config``, as ``(path, message)`` pairs; empty when valid.

    ``problem_type`` gates ``include_missing_labels_in_train_as`` (classification only). When it
    is absent or not a known type, that check is skipped — the problem_type check reports it.
    """
    if not isinstance(label_config, Mapping):
        return [("label_config", "label_config must be a mapping with a 'query'")]
    errors = _unknown_key_errors(label_config, "label_config", LABEL_CONFIG_KEYS)
    query = _query_of(label_config)
    if query is None:
        errors.append(("label_config.query", "label_config needs a 'query'"))
    else:
        for placeholder in ("{as_of_date}", "{label_timespan}"):
            if placeholder not in query:
                errors.append(
                    (
                        "label_config.query",
                        f"the label query must contain the {placeholder} placeholder",
                    )
                )
    fill = label_config.get(MISSING_LABELS_KEY)
    path = f"label_config.{MISSING_LABELS_KEY}"
    if fill is not None:
        if not isinstance(fill, bool):
            errors.append(
                (
                    path,
                    f"{MISSING_LABELS_KEY} must be true or false, got {fill!r} —"
                    + " false trains on unlabeled rows as 0, true as 1; leave it out to"
                    + " train on labeled rows only",
                )
            )
        elif (
            isinstance(problem_type, str)
            and problem_type not in _FILLABLE_PROBLEM_TYPES
        ):
            errors.append(
                (
                    path,
                    f"{MISSING_LABELS_KEY} applies to classification only, not"
                    + f" {problem_type!r}: a 0/1 fill would corrupt a continuous outcome,"
                    + " and a survival label has no outcome to fill",
                )
            )
    return errors


def _raise_on(errors: list[tuple[str, str]]) -> None:
    if errors:
        raise ValueError("; ".join(message for _, message in errors))


def validate_cohort_config(cohort_config: object) -> None:
    """Fail fast, before any build, on a malformed ``cohort_config``."""
    _raise_on(cohort_config_errors(cohort_config))


def validate_label_config(label_config: object, problem_type: object = None) -> None:
    """Fail fast, before any build, on a malformed ``label_config``."""
    _raise_on(label_config_errors(label_config, problem_type))


def missing_label_fill(label_config: Mapping[str, Any]) -> int | None:
    """The train-matrix fill for unlabeled rows: ``None`` (drop), ``0`` or ``1``.

    Assumes a validated config: ``None``/absent → ``None``, ``False`` → 0, ``True`` → 1.
    """
    fill = label_config.get(MISSING_LABELS_KEY)
    return None if fill is None else int(fill)


def labels_identity_config(label_config: Mapping[str, Any]) -> dict[str, Any]:
    """``label_config`` as it enters the labels derivation: without the train-only fill.

    The fill never changes a ``triage.labels`` row — it is applied when the train matrix is
    assembled — so it must not move the labels hash, nor the test matrices built on top of it.
    The train matrix carries the fill in its own identity instead.
    """
    return {k: v for k, v in label_config.items() if k != MISSING_LABELS_KEY}
