"""Feature-group partitioning + mixing strategies (ADR-0023). Pure logic, no DB.

Mirrors original triage's ``test_feature_group_mixer.py`` for the four strategies, plus the
featurizer-native ``source_entity`` partitioning and the explicit-glob path.
"""

import pytest

from triage.adapters.feature_groups import (
    FeatureSubset,
    matches_globs,
    mix_strategies,
    partition_features,
)

# DirtyDuck-shaped feature names: target-entity direct one-hots + child aggregations.
_FACILITY_COLS = [
    "facilities.facility_type=restaurant",
    "facilities.zip_code=60647",
]
_INSPECTION_COLS = [
    "COUNT(inspections.result|interval=P3M)",
    "COUNT(inspections.risk|interval=P6M)",
]
_ALIASES = ["facilities", "inspections"]


def _names(subsets):
    return [s.label for s in subsets]


def _by_groups(subsets):
    return {s.group_names: s.columns for s in subsets}


# ---------------------------------------------------------------- partitioning


def test_partition_by_source_entity_splits_facilities_and_inspections():
    groups = partition_features(_FACILITY_COLS + _INSPECTION_COLS, _ALIASES)
    assert set(groups) == {"facilities", "inspections"}
    assert groups["facilities"] == sorted(_FACILITY_COLS)
    assert groups["inspections"] == sorted(_INSPECTION_COLS)


def test_partition_source_entity_unmatched_column_is_loud():
    with pytest.raises(ValueError, match="could not map"):
        partition_features([*_FACILITY_COLS, "orphan_column"], _ALIASES)


def test_partition_source_entity_bare_target_var_falls_back_to_target():
    # featurizer names the target's PLAIN direct variables bare (no '<alias>.' prefix), e.g.
    # 'age'; these belong to the target entity, not an orphan error.
    cols = ["age", "COUNT(orders.amount|interval=P3650D)"]
    groups = partition_features(cols, ["customers", "orders"], target_alias="customers")
    assert groups == {
        "customers": ["age"],
        "orders": ["COUNT(orders.amount|interval=P3650D)"],
    }


def test_partition_explicit_globs():
    groups = partition_features(
        _FACILITY_COLS + _INSPECTION_COLS,
        _ALIASES,
        definitions={
            "facility_attrs": ["facilities.*"],
            "inspection_history": ["*(inspections.*"],
        },
    )
    assert set(groups) == {"facility_attrs", "inspection_history"}
    assert groups["facility_attrs"] == sorted(_FACILITY_COLS)
    assert groups["inspection_history"] == sorted(_INSPECTION_COLS)


def test_partition_explicit_unmatched_is_loud():
    with pytest.raises(ValueError, match="matches no feature_groups"):
        partition_features(
            _FACILITY_COLS, _ALIASES, definitions={"facility_attrs": ["nope.*"]}
        )


def test_partition_explicit_ambiguous_is_loud():
    with pytest.raises(ValueError, match="matches multiple groups"):
        partition_features(
            ["facilities.zip_code=60647"],
            _ALIASES,
            definitions={"a": ["facilities.*"], "b": ["*zip_code*"]},
        )


# -------------------------------------------- truncated names (the 63-byte cap)
#
# A generated feature name over PostgreSQL's 63-byte identifier cap is hash-truncated from
# the TAIL, so a glob aimed at an inner fragment matches the label and misses the column.
# The pair below is verbatim featurizer output (quotes stripped, as its manifest reports
# them) for a depth-2 config over a long-named entity — hardcoded so this module keeps its
# no-featurizer-import purity. Note the fragment 'frecuencia_cardiaca' survives ONLY in the
# label: it sits past the cut, which is precisely the case no glob could reach before.
_TRUNC_LABEL = (
    "ABS(consultas_ambulatorias.MAX(signos_vitales_registrados."
    "HOLT_WINTERS_LEVEL_7(signos_vitales_registrados."
    "frecuencia_cardiaca_en_reposo)|interval=P1M))"
)
_TRUNC_COL = "ABS(consultas_ambulatorias.MAX(signos_vitales_registra~01f94542"
_PLAIN_LABEL = "COUNT(consultas.frecuencia_cardiaca|interval=P1W)"
_LABELS = {_TRUNC_COL: _TRUNC_LABEL, _PLAIN_LABEL: _PLAIN_LABEL}

_CARDIAC_GLOB = "*frecuencia_cardiaca*"


def test_truncated_fixture_is_honest():
    # Guards the fixture itself: a "truncated" name that is not actually at the cap would
    # make every test below pass for the wrong reason.
    assert len(_TRUNC_COL.encode()) == 63
    assert len(_TRUNC_LABEL.encode()) > 63
    assert _CARDIAC_GLOB.strip("*") not in _TRUNC_COL


def test_truncated_column_groups_via_its_label():
    groups = partition_features(
        [_TRUNC_COL, _PLAIN_LABEL],
        [],
        definitions={"cardiac": [_CARDIAC_GLOB]},
        labels=_LABELS,
    )
    assert groups == {"cardiac": sorted([_TRUNC_COL, _PLAIN_LABEL])}


def test_truncated_column_without_labels_still_raises():
    # The map is what fixes it — proves the pass above is not incidental.
    with pytest.raises(ValueError, match="matches no feature_groups"):
        partition_features([_TRUNC_COL], [], definitions={"cardiac": [_CARDIAC_GLOB]})


def test_unmatched_error_shows_the_label():
    with pytest.raises(ValueError, match="label:"):
        partition_features(
            [_TRUNC_COL], [], definitions={"nope": ["*nomatch*"]}, labels=_LABELS
        )


def test_hand_written_physical_name_glob_still_matches():
    # Back-compat: pasting the ~hash tail was the only workaround before labels existed.
    groups = partition_features(
        [_TRUNC_COL], [], definitions={"hand": ["*~01f94542"]}, labels=_LABELS
    )
    assert groups == {"hand": [_TRUNC_COL]}


def test_label_and_column_hits_on_different_groups_stay_ambiguous():
    with pytest.raises(ValueError, match="matches multiple groups"):
        partition_features(
            [_TRUNC_COL],
            [],
            definitions={"by_label": [_CARDIAC_GLOB], "by_column": ["*~01f94542"]},
            labels=_LABELS,
        )


def test_labels_are_a_no_op_for_untruncated_columns():
    # The non-breaking claim, asserted: label == column ⇒ byte-identical partitioning.
    definitions = {
        "facility_attrs": ["facilities.*"],
        "inspection_history": ["*(inspections.*"],
    }
    cols = _FACILITY_COLS + _INSPECTION_COLS
    assert partition_features(cols, _ALIASES, definitions=definitions) == (
        partition_features(
            cols, _ALIASES, definitions=definitions, labels={c: c for c in cols}
        )
    )


def test_source_entity_path_ignores_labels():
    # group_by='source_entity' is immune by construction (head-keeping preserves the
    # leading '<alias>.' token), so a labels map must not perturb it.
    cols = _FACILITY_COLS + _INSPECTION_COLS
    lying = {c: "totally.different.name" for c in cols}
    assert partition_features(cols, _ALIASES) == partition_features(
        cols, _ALIASES, labels=lying
    )


def test_matches_globs_missing_column_falls_back_to_itself():
    # A column absent from the map is its own label — never a KeyError.
    assert matches_globs("plain.column", ["plain.*"], labels={"other": "x"})


def test_feature_groups_module_imports_no_featurizer():
    # The documented purity invariant ("no DB, no featurizer import"), enforced.
    from pathlib import Path

    import triage.adapters.feature_groups as fg

    source = Path(str(fg.__file__)).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "import featurizer" not in code
    assert "from featurizer" not in code


# ---------------------------------------------------------------- strategies

_GROUPS = {"facilities": _FACILITY_COLS, "inspections": _INSPECTION_COLS}


def test_strategy_all_one_subset_all_columns():
    subsets = mix_strategies(_GROUPS, ["all"])
    assert len(subsets) == 1
    assert subsets[0].group_names == ("facilities", "inspections")
    assert subsets[0].columns == tuple(sorted(_FACILITY_COLS + _INSPECTION_COLS))


def test_strategy_leave_one_in_each_group_alone():
    subsets = mix_strategies(_GROUPS, ["leave-one-in"])
    assert _by_groups(subsets) == {
        ("facilities",): tuple(sorted(_FACILITY_COLS)),
        ("inspections",): tuple(sorted(_INSPECTION_COLS)),
    }


def test_strategy_leave_one_out_drops_each_group():
    subsets = mix_strategies(_GROUPS, ["leave-one-out"])
    # 2 groups: leave-one-out leaves the OTHER single group.
    assert _by_groups(subsets) == {
        ("inspections",): tuple(sorted(_INSPECTION_COLS)),  # dropped facilities
        ("facilities",): tuple(sorted(_FACILITY_COLS)),  # dropped inspections
    }


def test_strategy_leave_one_out_single_group_skipped_then_kept_by_all():
    # leave-one-out of a single group is the empty set (skipped); 'all' still yields the group.
    subsets = mix_strategies({"only": ["only.a"]}, ["leave-one-out", "all"])
    assert _by_groups(subsets) == {("only",): ("only.a",)}


def test_strategy_leave_one_out_single_group_only_raises():
    with pytest.raises(ValueError, match="no usable subsets"):
        mix_strategies({"only": ["only.a"]}, ["leave-one-out"])


def test_strategy_all_combinations_powerset():
    subsets = mix_strategies(_GROUPS, ["all-combinations"])
    # 2 groups -> 2^2 - 1 = 3 subsets: {A},{B},{A,B}
    assert len(subsets) == 3
    assert {s.group_names for s in subsets} == {
        ("facilities",),
        ("inspections",),
        ("facilities", "inspections"),
    }


def test_all_combinations_cap_raises():
    big = {f"g{i}": [f"g{i}.x"] for i in range(7)}
    with pytest.raises(ValueError, match="above the cap"):
        mix_strategies(big, ["all-combinations"], all_combinations_max_groups=6)


def test_full_parity_dedupes_across_strategies():
    # all 4 strategies over 2 groups collapse to the 3 distinct subsets {A,B},{A},{B}.
    subsets = mix_strategies(
        _GROUPS, list(("all", "leave-one-in", "leave-one-out", "all-combinations"))
    )
    assert len(subsets) == 3
    assert {s.group_names for s in subsets} == {
        ("facilities", "inspections"),
        ("facilities",),
        ("inspections",),
    }
    # 'all' is seen first, so the full-set subset keeps its 'all' label.
    full = next(s for s in subsets if len(s.group_names) == 2)
    assert full.label == "all"


def test_subset_columns_are_sorted_union():
    s = FeatureSubset(label="x", group_names=("a", "b"), columns=("a.1", "b.2"))
    assert s.columns == tuple(sorted(s.columns))
