"""protected_groups ingestion tests — the bias_config block (ADR-0007, plan P2).

The invariant under test: a templated wide query (entity_id + one column per protected
attribute) melts to the long ``protected_groups`` shape, idempotently, per as_of_date —
and every malformed config fails LOUD before anything is built.
"""

from __future__ import annotations

import pytest

from triage.adapters.bias import (
    INTERVENTION_PRIMARY_METRIC,
    ingest_protected_groups,
    validate_bias_config,
)
from triage.adapters.run import validate_experiment_config

DATES = ["2026-01-01", "2026-02-01"]

# A self-contained wide query: two entities, race + sex, no source table needed. The
# {as_of_date} placeholder participates so each date's run is distinguishable.
WIDE_QUERY = """
    select entity_id, race, sex
    from (values (1, 'white', 'F'), (2, 'black', 'M'), (3, 'white', null))
         as t(entity_id, race, sex)
    where date '{as_of_date}' >= date '2026-01-01'
"""


def _rows(pool):
    with pool.connection() as conn:
        return conn.execute(
            "select entity_id, as_of_date::text as as_of_date, attribute_name,"
            " attribute_value from triage.protected_groups"
            " order by as_of_date, entity_id, attribute_name"
        ).fetchall()


def test_ingest_melts_wide_to_long_per_date(db_pool_greenfield):
    written = ingest_protected_groups(db_pool_greenfield, WIDE_QUERY, DATES)
    # 3 entities x 2 attributes, minus entity 3's NULL sex -> 5 rows per date
    assert written == 5 * len(DATES)
    rows = _rows(db_pool_greenfield)
    assert len(rows) == 10
    jan = [r for r in rows if r["as_of_date"] == "2026-01-01"]
    by_key = {(r["entity_id"], r["attribute_name"]): r["attribute_value"] for r in jan}
    assert by_key[(1, "race")] == "white"
    assert by_key[(2, "sex")] == "M"
    assert (3, "sex") not in by_key  # NULL attribute values are skipped, not stored


def test_ingest_is_idempotent_and_updates(db_pool_greenfield):
    ingest_protected_groups(db_pool_greenfield, WIDE_QUERY, DATES)
    ingest_protected_groups(db_pool_greenfield, WIDE_QUERY, DATES)
    assert len(_rows(db_pool_greenfield)) == 10  # upsert, not append

    # a changed attribute value on re-ingest wins (attribute_value is updated)
    changed = WIDE_QUERY.replace("(1, 'white', 'F')", "(1, 'asian', 'F')")
    ingest_protected_groups(db_pool_greenfield, changed, DATES[:1])
    rows = _rows(db_pool_greenfield)
    jan_race_1 = next(
        r
        for r in rows
        if r["entity_id"] == 1
        and r["attribute_name"] == "race"
        and r["as_of_date"] == "2026-01-01"
    )
    assert jan_race_1["attribute_value"] == "asian"


def test_ingest_fails_loud_on_contract_violations(db_pool_greenfield):
    with pytest.raises(ValueError, match="as_of_date.*placeholder"):
        ingest_protected_groups(
            db_pool_greenfield, "select 1 as entity_id, 'x' as race", DATES
        )
    with pytest.raises(ValueError, match="entity_id"):
        ingest_protected_groups(
            db_pool_greenfield,
            "select 1 as eid, 'x' as race where date '{as_of_date}' is not null",
            DATES,
        )
    with pytest.raises(ValueError, match="at least one protected-attribute"):
        ingest_protected_groups(
            db_pool_greenfield,
            "select 1 as entity_id where date '{as_of_date}' is not null",
            DATES,
        )


def test_validate_bias_config_fail_fast():
    ok = {
        "query": "select entity_id, race from t where d < '{as_of_date}'",
        "parameter": "100_abs",
    }
    validate_bias_config(ok)  # no raise

    with pytest.raises(ValueError, match="query"):
        validate_bias_config({"parameter": "100_abs"})
    with pytest.raises(ValueError, match="placeholder"):
        validate_bias_config(
            {"query": "select entity_id, race from t", "parameter": "100_abs"}
        )
    with pytest.raises(ValueError, match="parameter"):
        validate_bias_config({"query": ok["query"]})
    with pytest.raises(ValueError, match="tau"):
        validate_bias_config({**ok, "tau": 1.5})
    with pytest.raises(ValueError, match="intervention"):
        validate_bias_config({**ok, "intervention": "vindictive"})
    with pytest.raises(ValueError, match="ref_groups"):
        validate_bias_config({**ok, "ref_groups": ["white"]})


def test_intervention_metric_map_matches_fairness_tree():
    # docs/fairness.md: punitive -> FPR family, assistive -> FNR family,
    # representation -> selection rate. The wizard preselect keys off this map.
    assert INTERVENTION_PRIMARY_METRIC == {
        "punitive": "fpr",
        "assistive": "fnr",
        "representation": "selection_rate",
    }


def test_validate_experiment_config_reports_bias_errors():
    result = validate_experiment_config(
        {
            "bias_config": {
                "query": "select entity_id, race from t",  # missing placeholder
                "tau": 0,  # out of range
                "intervention": "nope",
                "ref_groups": ["white"],  # not a mapping
                # parameter missing
            }
        }
    )
    paths = {e["path"] for e in result["errors"]}
    assert {
        "bias_config.query",
        "bias_config.parameter",
        "bias_config.tau",
        "bias_config.intervention",
        "bias_config.ref_groups",
    } <= paths


def test_validate_experiment_config_accepts_good_bias_config():
    result = validate_experiment_config(
        {
            "bias_config": {
                "query": "select entity_id, race from t where d < '{as_of_date}'",
                "parameter": "100_abs",
                "tau": 0.8,
                "intervention": "punitive",
                "ref_groups": {"race": "white"},
            }
        }
    )
    assert not [e for e in result["errors"] if e["path"].startswith("bias_config")]
