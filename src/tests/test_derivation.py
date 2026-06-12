"""Tests for the derivation-hash primitive (ADR-0013, ADR-0016)."""

import datetime
import importlib.metadata
import uuid
from decimal import Decimal

import pytest

from triage.derivation import (
    VOLATILE,
    Derivation,
    canonical_json,
    derive,
    engine_versions_for,
)


def test_same_inputs_same_id():
    config = {"query": "select 1", "as_of_date": datetime.date(2026, 6, 1)}
    first = derive("cohort", config)
    second = derive("cohort", config)
    assert first == second
    assert first.cacheable


def test_key_order_is_irrelevant():
    one = derive("cohort", {"a": 1, "b": {"x": True, "y": None}})
    other = derive("cohort", {"b": {"y": None, "x": True}, "a": 1})
    assert one.id == other.id


def test_kind_enters_identity():
    config = {"query": "select 1"}
    assert derive("cohort", config).id != derive("labels", config).id


def test_config_change_changes_id():
    base = derive("cohort", {"query": "select 1"})
    changed = derive("cohort", {"query": "select 2"})
    assert base.id != changed.id


def test_parents_enter_identity_order_insensitively():
    parent_a = derive("cohort", {"q": "a"})
    parent_b = derive("labels", {"q": "b"})
    matrix_ab = derive("matrix", {}, parents=[parent_a, parent_b])
    matrix_ba = derive("matrix", {}, parents=[parent_b, parent_a])
    assert matrix_ab.id == matrix_ba.id

    other_parent = derive("labels", {"q": "c"})
    assert derive("matrix", {}, parents=[parent_a, other_parent]).id != matrix_ab.id


def test_source_pins_enter_identity():
    pinned_v1 = derive("cohort", {}, source_pins={"events": "v1"})
    pinned_v2 = derive("cohort", {}, source_pins={"events": "v2"})
    assert pinned_v1.id != pinned_v2.id
    assert pinned_v1.cacheable and pinned_v2.cacheable


def test_engine_versions_enter_identity():
    one = derive("model", {}, engine_versions={"triage-pg": "0.1"})
    other = derive("model", {}, engine_versions={"triage-pg": "0.2"})
    assert one.id != other.id


def test_unpinned_source_is_volatile():
    derivation = derive("cohort", {}, source_pins={"events": None})
    assert not derivation.cacheable
    # The sentinel stands in for the missing version inside the hash.
    assert derivation.id == derive("cohort", {}, source_pins={"events": VOLATILE}).id


def test_volatility_propagates_through_parents():
    volatile_parent = derive("cohort", {}, source_pins={"events": None})
    child = derive("matrix", {}, parents=[volatile_parent])
    assert not child.cacheable

    pinned_parent = derive("cohort", {}, source_pins={"events": "v1"})
    assert derive("matrix", {}, parents=[pinned_parent]).cacheable


def test_temporal_and_misc_types_normalize():
    config = {
        "as_of": datetime.datetime(2026, 6, 1, 12, 30),
        "span": datetime.timedelta(days=180),
        "rate": Decimal("0.15"),
        "run": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "tags": {"b", "a"},
    }
    assert derive("labels", config).id == derive("labels", dict(config)).id
    # date and datetime at midnight are distinct inputs, not conflated
    assert (
        derive("x", {"d": datetime.date(2026, 6, 1)}).id
        != derive("x", {"d": datetime.datetime(2026, 6, 1)}).id
    )


def test_set_normalization_is_order_free():
    assert canonical_json({"tags": {3, 1, 2}}) == canonical_json({"tags": {2, 3, 1}})


def test_non_finite_floats_are_rejected():
    with pytest.raises(TypeError, match="Non-finite"):
        _ = derive("x", {"value": float("nan")})
    with pytest.raises(TypeError, match="Non-finite"):
        _ = derive("x", {"value": float("inf")})


def test_unknown_types_are_rejected():
    class Opaque:
        pass

    with pytest.raises(TypeError, match="canonicalize"):
        _ = derive("x", {"value": Opaque()})


def test_non_string_keys_are_rejected():
    with pytest.raises(TypeError, match="keys must be strings"):
        _ = derive("x", {1: "one"})  # pyright: ignore[reportArgumentType]


def test_kind_must_be_nonempty():
    with pytest.raises(ValueError, match="non-empty"):
        _ = derive("", {})


def test_derivation_is_hashable_value_object():
    derivation = derive("cohort", {"q": "select 1"})
    assert isinstance(derivation, Derivation)
    assert derivation in {derivation}


def test_logical_id_ignores_engine_versions():
    one = derive("model", {"c": 1}, engine_versions={"scikit-learn": "1.5.1"})
    two = derive("model", {"c": 1}, engine_versions={"scikit-learn": "1.5.2"})
    assert one.id != two.id  # strict identity sees the drift
    assert one.logical_id == two.logical_id  # fallback chain does not


def test_logical_id_tracks_config_and_pins():
    base = derive("cohort", {"q": 1}, source_pins={"events": "v1"})
    other_config = derive("cohort", {"q": 2}, source_pins={"events": "v1"})
    other_pin = derive("cohort", {"q": 1}, source_pins={"events": "v2"})
    assert base.logical_id != other_config.logical_id
    assert base.logical_id != other_pin.logical_id


def test_logical_chain_survives_upstream_engine_drift():
    parent_v1 = derive("feature_group", {"g": 1}, engine_versions={"featurizer": "0.2"})
    parent_v2 = derive("feature_group", {"g": 1}, engine_versions={"featurizer": "0.3"})
    child_v1 = derive("matrix", {"m": 1}, parents=[parent_v1])
    child_v2 = derive("matrix", {"m": 1}, parents=[parent_v2])
    assert child_v1.id != child_v2.id  # drift propagates down the strict chain
    assert child_v1.logical_id == child_v2.logical_id  # fallback still matches


def test_engine_versions_for_cohort_is_triage_only():
    versions = engine_versions_for("cohort")
    assert set(versions) == {"triage-pg"}
    assert versions["triage-pg"]


def test_engine_versions_for_model_resolves_the_estimator():
    versions = engine_versions_for("model", "sklearn.tree.DecisionTreeClassifier")
    assert "triage-pg" in versions
    assert versions["scikit-learn"] == importlib.metadata.version("scikit-learn")


def test_engine_versions_for_model_requires_an_estimator():
    with pytest.raises(ValueError, match="estimator"):
        engine_versions_for("model")


def test_engine_versions_for_feature_group_needs_featurizer():
    try:
        featurizer_version = importlib.metadata.version("featurizer")
    except importlib.metadata.PackageNotFoundError:
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            engine_versions_for("feature_group")
    else:
        assert engine_versions_for("feature_group")["featurizer"] == featurizer_version
