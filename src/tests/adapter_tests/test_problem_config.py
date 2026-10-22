"""The problem-block contract (#21): accepted keys and the train-label fill's value rules.

``cohort_config`` and ``label_config`` enter identity, so a key nobody reads used to move the
experiment and labels hashes while doing nothing — ``include_missing_labels_in_train_as`` ported
from DSSG triage was the case that surfaced it. These tests pin what is accepted, what is
refused, and that the refusal names the key.
"""

from __future__ import annotations

import pytest

from triage.adapters.problem_config import (
    COHORT_CONFIG_KEYS,
    LABEL_CONFIG_KEYS,
    MISSING_LABELS_KEY,
    cohort_config_errors,
    label_config_errors,
    labels_identity_config,
    missing_label_fill,
    validate_cohort_config,
    validate_label_config,
)

COHORT = {
    "name": "active",
    "query": "select entity_id from e where d < {as_of_date}::date",
}
LABEL = {
    "name": "failed",
    "query": "select entity_id, 1 as outcome from ev"
    + " where {as_of_date}::date <= d and d < {as_of_date}::date + {label_timespan}",
}


def _paths(errors: list[tuple[str, str]]) -> set[str]:
    return {path for path, _ in errors}


def test_the_shipped_shapes_are_valid():
    assert cohort_config_errors(COHORT) == []
    assert (
        cohort_config_errors(
            {**COHORT, "description": "d", "forward_query": COHORT["query"]}
        )
        == []
    )
    assert label_config_errors(LABEL, "classification") == []
    assert label_config_errors({**LABEL, "description": "d"}, "regression") == []


def test_accepted_keys_are_exactly_the_ones_the_pipeline_reads():
    assert COHORT_CONFIG_KEYS == {"name", "description", "query", "forward_query"}
    assert LABEL_CONFIG_KEYS == {"name", "description", "query", MISSING_LABELS_KEY}


@pytest.mark.parametrize(
    ("block", "errors_of", "name"),
    [
        (COHORT, cohort_config_errors, "cohort_config"),
        (LABEL, lambda b: label_config_errors(b, "classification"), "label_config"),
    ],
)
def test_an_unknown_key_is_an_error_that_names_it_and_lists_the_accepted_ones(
    block, errors_of, name
):
    errors = errors_of({**block, "filepath": "cohort.sql"})

    assert _paths(errors) == {f"{name}.filepath"}
    (message,) = [m for _, m in errors]
    assert "unknown key 'filepath'" in message
    assert "accepted keys are description," in message  # sorted, so description leads


def test_a_misspelled_fill_key_is_refused_rather_than_ignored():
    """The DSSG port the issue started from, with a one-letter typo: it must not be silent."""
    errors = label_config_errors(
        {**LABEL, "include_missing_label_in_train_as": False}, "classification"
    )
    assert _paths(errors) == {"label_config.include_missing_label_in_train_as"}


def test_forward_query_belongs_to_the_cohort_only():
    errors = label_config_errors({**LABEL, "forward_query": "x"}, "classification")
    assert _paths(errors) == {"label_config.forward_query"}


def test_the_query_rules_are_unchanged():
    assert _paths(cohort_config_errors({"name": "x"})) == {"cohort_config.query"}
    assert _paths(cohort_config_errors({"query": "select 1"})) == {
        "cohort_config.query"
    }
    assert [m for _, m in label_config_errors({"query": "select {as_of_date}"})] == [
        "the label query must contain the {label_timespan} placeholder"
    ]
    assert _paths(cohort_config_errors("select 1")) == {"cohort_config"}


@pytest.mark.parametrize("value", [True, False, None])
def test_the_fill_accepts_booleans_and_null(value):
    assert (
        label_config_errors({**LABEL, MISSING_LABELS_KEY: value}, "classification")
        == []
    )


@pytest.mark.parametrize("value", [0, 1, "false", "true", 0.0])
def test_the_fill_refuses_anything_but_a_boolean(value):
    """DSSG raised on anything but None/False/True; 0 and 1 are not booleans in YAML either."""
    errors = label_config_errors({**LABEL, MISSING_LABELS_KEY: value}, "classification")
    assert _paths(errors) == {f"label_config.{MISSING_LABELS_KEY}"}
    assert "must be true or false" in errors[0][1]


@pytest.mark.parametrize(
    "problem_type", ["regression", "regression_ranking", "survival"]
)
def test_the_fill_is_classification_only(problem_type):
    errors = label_config_errors({**LABEL, MISSING_LABELS_KEY: False}, problem_type)
    assert _paths(errors) == {f"label_config.{MISSING_LABELS_KEY}"}
    assert "classification only" in errors[0][1]


def test_the_fill_check_waits_for_a_known_problem_type():
    """An absent or unknown problem_type is reported by its own check, not twice here."""
    assert label_config_errors({**LABEL, MISSING_LABELS_KEY: False}, None) == []


def test_the_validators_raise_with_every_message():
    with pytest.raises(ValueError, match="unknown key 'filepath'") as excinfo:
        validate_label_config({"filepath": "x.sql"}, "classification")
    assert "label_config needs a 'query'" in str(excinfo.value)
    with pytest.raises(ValueError, match="unknown key 'nme'"):
        validate_cohort_config({**COHORT, "nme": "typo"})
    validate_cohort_config(COHORT)
    validate_label_config({**LABEL, MISSING_LABELS_KEY: True}, "classification")


@pytest.mark.parametrize(("value", "fill"), [(None, None), (False, 0), (True, 1)])
def test_missing_label_fill(value, fill):
    config = dict(LABEL) if value is None else {**LABEL, MISSING_LABELS_KEY: value}
    assert missing_label_fill(config) == fill


def test_the_labels_identity_never_carries_the_fill():
    assert labels_identity_config({**LABEL, MISSING_LABELS_KEY: False}) == LABEL
    assert labels_identity_config(LABEL) == LABEL
