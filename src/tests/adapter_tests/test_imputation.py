"""Tests for triage.adapters imputation policy (docs/adapter-spec.md §3, ADR-0009)."""

import pytest
from pydantic import ValidationError

from triage.adapters import ImputationPolicy, ImputationRule
from triage.derivation import canonical_json


def test_fit_free_rules_classified():
    for rule_type in ("zero", "zero_noflag", "null_category"):
        rule = ImputationRule(type=rule_type)
        assert rule.kind == "fit_free"
        assert rule.fits_on_train is False
    constant = ImputationRule(type="constant", value=0)
    assert constant.kind == "fit_free"
    assert constant.fits_on_train is False


def test_fit_based_rules_classified():
    for rule_type in ("mean", "median", "mode", "binary_mode"):
        rule = ImputationRule(type=rule_type)
        assert rule.kind == "fit_based"
        assert rule.fits_on_train is True


def test_error_rule_classified():
    rule = ImputationRule(type="error")
    assert rule.kind == "error"
    assert rule.fits_on_train is False


def test_constant_requires_value():
    with pytest.raises(ValidationError):
        ImputationRule(type="constant")
    # explicit value is accepted (int or str)
    assert ImputationRule(type="constant", value=137).value == 137
    assert ImputationRule(type="constant", value="red").value == "red"


def test_nonconstant_rejects_value():
    with pytest.raises(ValidationError):
        ImputationRule(type="mean", value=5)


def test_unknown_rule_type_rejected():
    with pytest.raises(ValidationError):
        ImputationRule.model_validate({"type": "bogus"})


def test_extra_key_rejected():
    with pytest.raises(ValidationError):
        ImputationRule.model_validate({"type": "zero", "unexpected": 1})


def test_policy_resolve_with_all_fallback():
    policy = ImputationPolicy.model_validate(
        {"all": {"type": "constant", "value": 0}, "max": {"type": "mean"}}
    )
    # explicit metric rule wins
    assert policy.resolve("max").type == "mean"
    # unspecified metric falls back to 'all'
    assert policy.resolve("count").type == "constant"
    assert policy.resolve("count").value == 0


def test_policy_resolve_without_all_raises():
    policy = ImputationPolicy.model_validate({"max": {"type": "mean"}})
    with pytest.raises(KeyError):
        policy.resolve("count")


def test_policy_requires_fit():
    fit_free = ImputationPolicy.model_validate({"all": {"type": "zero"}})
    assert fit_free.requires_fit() is False
    mixed = ImputationPolicy.model_validate(
        {"all": {"type": "zero"}, "max": {"type": "mean"}}
    )
    assert mixed.requires_fit() is True


def test_empty_policy_rejected():
    with pytest.raises(ValidationError):
        ImputationPolicy.model_validate({})


def test_canonical_is_deterministic_across_key_order():
    a = ImputationPolicy.model_validate(
        {"all": {"type": "constant", "value": 0}, "max": {"type": "mean"}}
    ).canonical()
    b = ImputationPolicy.model_validate(
        {"max": {"type": "mean"}, "all": {"type": "constant", "value": 0}}
    ).canonical()
    assert a == b
    assert canonical_json(a) == canonical_json(b)
    # the constant's value survives into the canonical form
    assert a["all"] == {"type": "constant", "value": 0}
    assert a["max"] == {"type": "mean"}


def test_parses_inherited_aggregates_block():
    # The inherited DSSG-style aggregates_imputation shape: 'all' is constant:0 (fit-free),
    # 'max' is mean (fit-based) → a mixed policy. ImputationPolicy must still parse it.
    block = {
        "all": {"type": "constant", "value": 0},
        "max": {"type": "mean"},
    }
    policy = ImputationPolicy.model_validate(block)
    assert policy.resolve("max").kind == "fit_based"
    assert policy.requires_fit() is True
