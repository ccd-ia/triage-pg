"""Tests for the derivation-hash primitive (ADR-0013)."""

import datetime
import uuid
from decimal import Decimal

import pytest

from triage.derivation import VOLATILE, Derivation, canonical_json, derive


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
