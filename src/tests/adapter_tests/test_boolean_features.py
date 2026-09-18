"""Boolean feature columns survive into the design matrix as 0/1 (#12).

``_numeric_feature_columns`` dropped every column whose dtype was not in ``_numeric_dtypes()``,
and ``pl.Boolean`` is not in that tuple. A PostgreSQL ``boolean`` is already a 0/1 feature, so the
drop lost a column the config explicitly declared — quietly, in the way that matters:
``analyze-config`` counted it (it is in featurizer's plan), the config declared it, the run
trained, and the model simply never saw it.

The companion assertion matters as much: ``Date`` and ``String`` are still dropped, because those
genuinely need a user decision. The fix is a cast, not a widening of the guard.
"""

from __future__ import annotations

import copy
from typing import Any

from tests.adapter_tests.test_run_orchestration import (
    _experiment_config,
    _seed_source,
)
from triage.adapters.run import run_experiment
from triage.profiles.storage import LocalStorage

BOOLEAN_FEATURE = "is_vip"


def _config_with_boolean() -> dict[str, Any]:
    """A plain boolean passthrough — no ``role: categorical``, no vocabulary.

    This is the shape the issue reports as lost: declaring ``type: boolean`` does not change what
    PostgreSQL returns, so the column arrives as ``Boolean`` and was dropped. The reporter's
    workaround was to one-hot it instead, which doubles the column count and renames it.
    """
    config = copy.deepcopy(_experiment_config())
    config["feature_config"]["entities"][0]["variables"][BOOLEAN_FEATURE] = {
        "type": "boolean"
    }
    return config


def _seed_with_boolean(engine) -> None:
    _seed_source(engine)
    with engine.connection() as conn:
        conn.execute("alter table customers add column is_vip boolean")
        conn.execute("update customers set is_vip = (customer_id % 2 = 0)")


def _matrix_rows(engine) -> list[dict[str, Any]]:
    with engine.connection() as conn:
        return conn.execute(
            "select matrix_kind, feature_names, storage_uri from triage.matrices"
            " order by matrix_kind"
        ).fetchall()


def test_boolean_feature_reaches_the_matrix_as_numeric(
    db_pool_greenfield, tmp_path
) -> None:
    """The declared boolean is a feature column, stored 0/1 rather than dropped."""
    import polars as pl

    engine = db_pool_greenfield
    _seed_with_boolean(engine)
    storage = str(tmp_path / "store")
    run_experiment(
        engine,
        _config_with_boolean(),
        storage=LocalStorage(),
        storage_root=storage,
        random_seed=42,
    )

    rows = _matrix_rows(engine)
    assert rows, "the experiment built no matrices"
    for row in rows:
        names = list(row["feature_names"])
        assert BOOLEAN_FEATURE in names, (
            f"the declared boolean is missing from the {row['matrix_kind']} matrix's"
            f" feature_names — it was dropped: {names}"
        )
        frame = pl.read_parquet(row["storage_uri"])
        assert frame.schema[BOOLEAN_FEATURE] in (
            pl.Int8,
            pl.Int16,
            pl.Int32,
            pl.Int64,
        ), "the boolean must be cast to an integer 0/1, not left as Boolean"
        values = set(frame.get_column(BOOLEAN_FEATURE).to_list())
        assert values <= {0, 1}, f"expected 0/1 values, got {sorted(values)}"


def test_the_target_temporal_ix_is_excluded_at_the_source() -> None:
    """``signup_date`` — the target entity's clock — is never a feature column (#12).

    featurizer emits it as a Date-typed feature although it is not a declared variable. The old
    guard dropped it downstream and advised "mark it role:identifier in the featurizer config",
    which cannot be followed: there is no variable to mark. It is now excluded by name, where the
    config is available to say which column is the target's clock.

    Asserted on ``_feature_columns`` rather than on a built matrix, because a built matrix cannot
    tell the two apart — the guard dropped the Date too, so ``feature_names`` looked identical
    either way. What changed is that the column no longer reaches the guard, and the user no
    longer gets advice they cannot act on.
    """
    from triage.adapters.matrix import _feature_columns, _target_temporal_ix

    config = _config_with_boolean()["feature_config"]
    assert _target_temporal_ix(config) == "signup_date"
    # A config naming no target entity must not crash the lookup.
    assert _target_temporal_ix({}) is None
    assert _target_temporal_ix(None) is None

    columns = [
        "as_of_date",
        "entity_id",
        "signup_date",
        "age",
        "is_vip",
        "age__missing",
    ]
    kept = _feature_columns(columns, "entity_id", _target_temporal_ix(config))
    assert kept == ["age", "is_vip"], (
        "the target's temporal_ix must be excluded with the keys, not left to the"
        " non-numeric guard to drop with unfollowable advice"
    )
    # Without the config it is still let through — the exclusion is config-driven, not a
    # hardcoded name, so an entity whose clock is called something else is covered too.
    assert "signup_date" in _feature_columns(columns, "entity_id")


def test_non_numeric_columns_that_need_a_decision_are_still_dropped() -> None:
    """A Date or String feature is still dropped — the fix is a cast, not a widened guard."""
    import polars as pl

    from triage.adapters.matrix import _cast_boolean_features, _numeric_feature_columns

    frame = pl.DataFrame(
        {
            "flag": [True, False, True],
            "when": [
                __import__("datetime").date(2014, 1, 1),
                __import__("datetime").date(2014, 2, 1),
                __import__("datetime").date(2014, 3, 1),
            ],
            "label_text": ["a", "b", "c"],
            "amount": [1.0, 2.0, 3.0],
        }
    )
    columns = ["flag", "when", "label_text", "amount"]

    frame, cast = _cast_boolean_features(frame, columns)
    assert cast == ["flag"]
    assert frame.schema["flag"] == pl.Int8

    kept = _numeric_feature_columns(frame, columns)
    assert kept == [
        "flag",
        "amount",
    ], (
        "the boolean survives the cast; the Date and the String still need a user decision"
    )
