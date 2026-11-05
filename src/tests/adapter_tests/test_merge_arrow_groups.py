"""Re-joining featurizer's column groups (wide-config sharding). Pure logic, no DB.

A config wider than PostgreSQL's 1664-entry target list comes back from
``Featurizer.to_arrow`` as an ``OrderedDict`` of column-group Tables, and
``_merge_arrow_groups`` re-joins them on ``(as_of_date, <target id>)``. Every group carries
the target's full identifier columns, not only its id: the keys of the relationships the
target is a parent of travel with it (featurizer's ``Entity.identifier_columns``). The tables
below have that shape: ``(as_of_date, <id>, <relationship key>, <features>)``.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from typing import Any

import pyarrow as pa

from triage.adapters.matrix import _merge_arrow_groups

_DATES = [date(2025, 6, 1), date(2025, 6, 1), date(2025, 9, 1)]
_IDS = [1, 2, 1]
_SITES = [10, 20, 10]  # the relationship key every group carries next to the id


def _group(features: dict[str, list[float]], *, carried: bool = True) -> pa.Table:
    columns: dict[str, list] = {"as_of_date": _DATES, "person_id": _IDS}
    if carried:
        columns["site_id"] = _SITES
    columns.update(features)
    return pa.table(columns)


def _groups(*tables: pa.Table) -> OrderedDict[str, pa.Table]:
    return OrderedDict((f"group_{i:03d}", table) for i, table in enumerate(tables))


def test_three_groups_with_a_carried_relationship_key_merge():
    # Before the fix the second join produced `site_id_right` and the third raised
    # polars' DuplicateError ("column with name 'site_id_right' already exists").
    merged = _merge_arrow_groups(
        _groups(
            _group({"f_a": [1.0, 2.0, 3.0]}),
            _group({"f_b": [4.0, 5.0, 6.0]}),
            _group({"f_c": [7.0, 8.0, 9.0]}),
        ),
        "person_id",
    )

    assert merged.column_names == [
        "as_of_date",
        "person_id",
        "site_id",
        "f_a",
        "f_b",
        "f_c",
    ]
    assert merged.column("site_id").to_pylist() == _SITES
    assert merged.column("f_c").to_pylist() == [7.0, 8.0, 9.0]


def test_two_groups_do_not_leave_a_duplicate_key_column():
    # Two groups never raised: the carried key came out twice, as `site_id` and
    # `site_id_right`, and the copy went on to the design matrix as a feature.
    merged = _merge_arrow_groups(
        _groups(_group({"f_a": [1.0, 2.0, 3.0]}), _group({"f_b": [4.0, 5.0, 6.0]})),
        "person_id",
    )

    assert merged.column_names == ["as_of_date", "person_id", "site_id", "f_a", "f_b"]


def test_merged_groups_equal_the_single_query_table():
    # Sharding is an execution detail: the merged frame is the table one query would return.
    single = _group({"f_a": [1.0, 2.0, 3.0], "f_b": [4.0, 5.0, 6.0]})
    merged = _merge_arrow_groups(
        _groups(_group({"f_a": [1.0, 2.0, 3.0]}), _group({"f_b": [4.0, 5.0, 6.0]})),
        "person_id",
    )

    assert merged.to_pydict() == single.to_pydict()


def test_groups_that_carry_only_the_join_keys_still_merge():
    # A target with no relationship keys: the groups share (as_of_date, id) and nothing else.
    merged = _merge_arrow_groups(
        _groups(
            _group({"f_a": [1.0, 2.0, 3.0]}, carried=False),
            _group({"f_b": [4.0, 5.0, 6.0]}, carried=False),
            _group({"f_c": [7.0, 8.0, 9.0]}, carried=False),
        ),
        "person_id",
    )

    assert merged.column_names == ["as_of_date", "person_id", "f_a", "f_b", "f_c"]


def test_rows_join_on_the_keys_not_on_position():
    # The later group arrives in another row order; each feature lands on its own key.
    reordered = pa.table(
        {
            "as_of_date": [_DATES[2], _DATES[0], _DATES[1]],
            "person_id": [_IDS[2], _IDS[0], _IDS[1]],
            "site_id": [_SITES[2], _SITES[0], _SITES[1]],
            "f_b": [6.0, 4.0, 5.0],
        }
    )
    merged = _merge_arrow_groups(
        _groups(_group({"f_a": [1.0, 2.0, 3.0]}), reordered), "person_id"
    )

    assert merged.column("f_b").to_pylist() == [4.0, 5.0, 6.0]


# ------------------------------------------------- end to end, through featurizer

_MEASURES = [f"m{i:03d}" for i in range(100)]
_INTERVALS = ["P30D", "P90D", "P180D", "P1Y", "P2Y", "P3Y", "P5Y", "P10Y"]
_AGGREGATIONS = ["sum", "mean", "max", "min"]
_AS_OF = date(2013, 1, 1)


def _wide_config() -> dict[str, Any]:
    """A target with a relationship key of its own, wide enough to shard into 3 groups.

    ``customers.region_id`` is the key of the customers -> region_stats relationship, so
    featurizer carries it next to ``customer_id`` in every column group. 100 measures x 8
    intervals x 4 aggregations is 3,200 columns from ``orders`` alone: over PostgreSQL's
    1,664-entry target list, and over two of featurizer's 1,400-column groups.
    """
    return {
        "target": "customers",
        "max_depth": 2,
        "intervals": _INTERVALS,
        "aggregations": _AGGREGATIONS,
        "transformations": ["identity"],
        "entities": [
            {
                "alias": "customers",
                "id": "customer_id",
                "table": "customers",
                "variables": {"age": {"type": "numeric"}},
            },
            {
                "alias": "orders",
                "id": "order_id",
                "table": "orders",
                "temporal_ix": "order_date",
                "variables": {name: {"type": "numeric"} for name in _MEASURES},
            },
            {
                "alias": "region_stats",
                "id": "stat_id",
                "table": "region_stats",
                "temporal_ix": "stat_date",
                "variables": {"visits": {"type": "numeric"}},
            },
        ],
        "relationships": [
            {
                "parent": {"entity": "customers", "key": "customer_id"},
                "child": {"entity": "orders", "key": "customer_id"},
            },
            {
                "parent": {"entity": "customers", "key": "region_id"},
                "child": {"entity": "region_stats", "key": "region_id"},
            },
        ],
    }


def _seed_wide(conn) -> None:
    measures = ", ".join(f"{name} double precision" for name in _MEASURES)
    values = ", ".join(f"g * {i + 1}" for i in range(len(_MEASURES)))
    conn.execute(
        "create table customers"
        " (customer_id bigint primary key, region_id bigint, age int)"
    )
    conn.execute(
        "insert into customers select g, 10 * (1 + g % 2), 30 + g"
        " from generate_series(1, 6) as g"
    )
    conn.execute(
        "create table orders (order_id bigint primary key, customer_id bigint,"
        f" order_date date, {measures})"
    )
    conn.execute(
        "insert into orders select g, 1 + g % 5, date '2012-01-01' + g,"
        f" {values} from generate_series(1, 20) as g"
    )
    conn.execute(
        "create table region_stats (stat_id bigint primary key, region_id bigint,"
        " stat_date date, visits double precision)"
    )
    conn.execute(
        "insert into region_stats select g, 10 * (1 + g % 2), date '2012-06-01' + g, g"
        " from generate_series(1, 8) as g"
    )


def test_a_wide_config_with_a_relationship_key_builds_its_features(db_pool):
    """featurizer's own groups, merged on the path `run` and `check leakage` share."""
    from triage.diagnostics.leakage import _FeatureBuilder

    builder = _FeatureBuilder(_wide_config())
    sharder = builder.featurizer._sharder()
    # The two premises of the bug, read from featurizer: 3+ groups, a carried key.
    assert sharder.n_groups >= 3
    assert sharder.key_columns == ["as_of_date", "customer_id", "region_id"]

    with db_pool.connection() as conn:
        _seed_wide(conn)
        frame = builder.build(conn, _AS_OF, range(1, 7))

    assert [name for name in frame.columns if name.endswith("_right")] == []
    assert frame.columns[:3] == ["as_of_date", "customer_id", "region_id"]
    assert frame.get_column("customer_id").to_list() == [1, 2, 3, 4, 5, 6]
    assert frame.get_column("region_id").to_list() == [20, 10, 20, 10, 20, 10]
    assert len(frame.columns) > 3200
