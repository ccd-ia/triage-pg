"""The leakage check (ADR-0034): harness facts, cut plan, comparisons, discovery, planted leaks.

Groups, selectable with ``-k``:

* ``harness`` — the four facts the check rests on (a DELETE inside the check's transaction is
  invisible elsewhere and undone by ROLLBACK; featurizer never commits a caller's connection;
  foreign keys and triggers behave), plus the commit guard and the no-commit source guard.
* ``plan`` / ``compare`` — the cut plan and the comparisons, with no database.
* ``discovery`` / ``fingerprint`` — relation discovery and the before/after proof.
* ``cli`` — ``triage check leakage`` on a clean synthetic experiment and on one planted
  leak per component, plus its exit-2 paths and help text.
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
import pytest
import yaml
from psycopg import pq, sql
from typer.testing import CliRunner

import triage.cli as cli
from triage.adapters.matrix import _featurizer_config_yaml, _materialize_pairs
from triage.diagnostics import leakage
from triage.diagnostics.leakage import (
    Check,
    CutKey,
    LeakageCheckError,
    SourceSpec,
    check_leakage,
    compare_cohort,
    compare_features,
    compare_labels,
    delete_after,
    discover_relations,
    fingerprints,
    plan_cuts,
)
from triage.util.db import libpq_conninfo

runner = CliRunner()

# --------------------------------------------------------------------------------------
# The synthetic experiment
# --------------------------------------------------------------------------------------

LABEL_TIMESPAN = "6 months"
# as_of_dates {2013-01-01, 2013-07-01, 2014-01-01, 2014-07-01}, 6-month labels.
TEMPORAL_CONFIG = {
    "feature_start_time": "2013-01-01",
    "feature_end_time": "2015-01-01",
    "label_start_time": "2013-01-01",
    "label_end_time": "2015-01-01",
    "model_update_frequency": "6 months",
    "training_as_of_date_frequencies": "6 months",
    "test_as_of_date_frequencies": "6 months",
    "max_training_histories": "6 months",
    "test_durations": "0 days",
    "label_timespans": LABEL_TIMESPAN,
}
AS_OF_DATES = [date(2013, 1, 1), date(2013, 7, 1), date(2014, 1, 1), date(2014, 7, 1)]

# The literal '%' proves a bare SELECT needs no '%%' doubling.
CLEAN_COHORT = (
    "select entity_id from entities"
    " where start_time <= {as_of_date}::date and name like 'e%'"
)
CLEAN_LABELS = (
    "select entity_id, max(failed) as outcome from events"
    " where occurred_at >= {as_of_date}::date"
    " and occurred_at < {as_of_date}::date + {label_timespan}"
    " group by entity_id"
)
# Label window with no upper bound: {label_timespan} is present but does not bound it.
LEAKY_LABELS = (
    "select entity_id, max(failed) as outcome from events"
    " where occurred_at >= {as_of_date}::date and {label_timespan} > interval '0 days'"
    " group by entity_id"
)
# Entities selected by activity after the as_of_date.
LEAKY_COHORT = (
    "select distinct entity_id from events where occurred_at > {as_of_date}::date"
)
UNDECLARED_COHORT = (
    "select e.entity_id from entities as e inner join flags as f using (entity_id)"
    " where e.start_time <= {as_of_date}::date and f.active"
)

# Entity start dates: three entities arrive after the first as_of_date.
_STARTS = {
    1: date(2012, 1, 1),
    2: date(2012, 1, 1),
    3: date(2012, 3, 1),
    4: date(2012, 6, 1),
    5: date(2012, 9, 1),
    6: date(2013, 3, 1),
    7: date(2013, 9, 15),
    8: date(2014, 2, 1),
}
_LATE_DAYS = 30


def _featurizer_config() -> dict[str, Any]:
    """entities (target) ⋈ events; COUNT/SUM of events.amount, all-time and last 90 days."""
    return {
        "target": "entities",
        "max_depth": 2,
        "intervals": ["P90D"],
        "aggregations": ["count", "sum"],
        "transformations": ["identity"],
        "entities": [
            {
                "alias": "entities",
                "id": "entity_id",
                "table": "entities",
                "variables": {"size": {"type": "numeric"}},
            },
            {
                "alias": "events",
                "id": "event_id",
                "table": "events",
                "temporal_ix": "occurred_at",
                "variables": {"amount": {"type": "numeric"}},
            },
        ],
        "relationships": [
            {
                "parent": {"entity": "entities", "key": "entity_id"},
                "child": {"entity": "events", "key": "entity_id"},
            }
        ],
    }


def _config(
    *,
    cohort: str = CLEAN_COHORT,
    labels: str = CLEAN_LABELS,
    events_knowledge_date: str = "occurred_at",
) -> dict[str, Any]:
    return {
        "problem_type": "classification",
        "temporal_config": TEMPORAL_CONFIG,
        "cohort_config": {"query": cohort},
        "label_config": {"query": labels},
        "feature_config": _featurizer_config(),
        "grid_config": {"sklearn.tree.DecisionTreeClassifier": {"max_depth": [3]}},
        "sources": [
            {
                "name": "entities",
                "relation": "entities",
                "knowledge_date_column": "start_time",
                "role": "entity",
            },
            {
                "name": "events",
                "relation": "events",
                "knowledge_date_column": events_knowledge_date,
                "role": "event",
            },
        ],
    }


def _seed(pool) -> None:
    """Eight entities, events every 20 days from each start to mid-2015.

    ``known_at`` is ``occurred_at`` + 30 days: the late-arriving data of planted leak 3. Every
    as_of_date has events in the 30 days before it, so a late arrival moves the features.
    """
    events = []
    event_id = 0
    for entity_id, start in _STARTS.items():
        day = start
        i = 0
        while day < date(2015, 6, 1):
            event_id += 1
            events.append(
                (
                    event_id,
                    entity_id,
                    day,
                    day + timedelta(days=_LATE_DAYS),
                    float(entity_id * 10 + i % 7),
                    int((entity_id + i) % 3 == 0),
                )
            )
            day += timedelta(days=20)
            i += 1
    with pool.connection() as conn:
        conn.execute(
            "create table entities (entity_id bigint primary key, start_time date not null,"
            " size integer, name text)"
        )
        with conn.cursor() as cur:
            cur.executemany(
                "insert into entities (entity_id, start_time, size, name)"
                " values (%s, %s, %s, %s)",
                [(e, s, e * 3, f"e{e}") for e, s in _STARTS.items()],
            )
        conn.execute(
            "create table events (event_id bigint primary key, entity_id bigint not null,"
            " occurred_at date not null, known_at date not null,"
            " amount double precision, failed integer)"
        )
        with conn.cursor() as cur:
            cur.executemany(
                "insert into events (event_id, entity_id, occurred_at, known_at, amount,"
                " failed) values (%s, %s, %s, %s, %s, %s)",
                events,
            )
        conn.execute("create table flags (entity_id bigint, active boolean)")
        conn.execute(
            "insert into flags (entity_id, active) select entity_id, true from entities"
        )


@pytest.fixture(name="seeded")
def fixture_seeded(db_url, db_pool):
    """The synthetic Sources in a fresh database; yields the database URL."""
    _seed(db_pool)
    return db_url


def _connect(db_url: str) -> psycopg.Connection[Any]:
    return psycopg.connect(libpq_conninfo(db_url), autocommit=False)


def _count(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute(
        sql.SQL("select count(*) from {}").format(sql.Identifier(table))
    ).fetchone()
    assert row is not None
    return int(row[0])


_EVENTS = SourceSpec("events", "events", "occurred_at", "event")
_ENTITIES = SourceSpec("entities", "entities", "start_time", "entity")


# --------------------------------------------------------------------------------------
# harness — the facts the check rests on
# --------------------------------------------------------------------------------------


def test_harness_delete_is_invisible_elsewhere_and_undone_by_rollback(seeded):
    a, b = _connect(seeded), _connect(seeded)
    try:
        total = _count(b, "events")
        b.rollback()
        a.execute("delete from events where occurred_at >= %s", (date(2014, 1, 1),))
        assert _count(a, "events") < total
        assert _count(b, "events") == total  # MVCC: B never sees A's uncommitted delete
        b.rollback()
        a.rollback()
        assert _count(a, "events") == total
    finally:
        a.close()
        b.close()


def test_harness_tripwire_featurizer_to_arrow_never_commits_the_callers_connection(
    seeded, tmp_path
):
    """featurizer must run on the caller's transaction and never commit it.

    If this fails after a featurizer bump, the leakage check is no longer safe to run on
    that version: its deletes could become permanent. The commit guard would refuse such a
    commit, but the check would stop working. Fix featurizer or pin the previous version.
    """
    from importlib.metadata import version

    from featurizer import Featurizer

    a, b = _connect(seeded), _connect(seeded)
    path = tmp_path / "tripwire.yaml"
    try:
        total = _count(b, "events")
        b.rollback()
        a.execute("delete from events where occurred_at >= %s", (date(2014, 1, 1),))
        remaining = _count(a, "events")
        with a.cursor() as cur:
            _materialize_pairs(cur, [(date(2014, 1, 1), 1), (date(2014, 1, 1), 2)])
        path.write_text(_featurizer_config_yaml(_featurizer_config(), paired=True))
        table = Featurizer(str(path), validate=True).to_arrow(connection=a, impute=True)

        installed = version("featurizer")
        assert a.info.transaction_status == pq.TransactionStatus.INTRANS, (
            f"featurizer {installed} ended the caller's transaction in to_arrow"
        )
        assert _count(b, "events") == total, (
            f"featurizer {installed} committed the caller's DELETE"
        )
        assert _count(a, "events") == remaining  # the cut is still in effect on A
        assert table.num_rows == 2
    finally:
        a.rollback()
        a.close()
        b.close()


def test_harness_foreign_key_events_cut_before_entities(db_url, db_pool):
    with db_pool.connection() as conn:
        conn.execute(
            "create table ents (entity_id bigint primary key, start_time date not null)"
        )
        conn.execute(
            "create table evs (event_id bigint primary key,"
            " entity_id bigint not null references ents on delete no action,"
            " occurred_at date not null)"
        )
        conn.execute(
            "insert into ents values (1, date '2013-01-01'), (2, date '2013-06-01'),"
            " (3, date '2014-01-01')"
        )
        # Event 30 is known before its entity 3: the case the check must report.
        conn.execute(
            "insert into evs values (10, 1, date '2013-02-01'), (20, 2, date '2013-07-01'),"
            " (30, 3, date '2013-12-01')"
        )
    evs = SourceSpec("evs", "evs", "occurred_at", "event")
    ents = SourceSpec("ents", "ents", "start_time", "entity")
    conn = _connect(db_url)
    try:
        # Entities listed first on purpose: the harness still deletes events first, so
        # entity 3 goes after its event 30. Entity first would break the foreign key.
        deleted = delete_after(
            conn, [ents, evs], CutKey(datetime(2013, 6, 15), True, False)
        )
        assert deleted == {"evs": 2, "ents": 1}
        conn.rollback()

        # Event 30 (2013-12-01) survives a 2013-12-15 cut; its entity (2014-01-01) does not.
        with pytest.raises(LeakageCheckError, match="foreign key") as caught:
            delete_after(conn, [ents, evs], CutKey(datetime(2013, 12, 15), True, False))
        assert "Source 'ents' (ents)" in str(caught.value)
        assert isinstance(caught.value.__cause__, psycopg.errors.ForeignKeyViolation)
        # Rolled back to the savepoint: the transaction is still usable.
        assert conn.info.transaction_status == pq.TransactionStatus.INTRANS
        assert _count(conn, "ents") == 3
    finally:
        conn.rollback()
        conn.close()


def test_harness_after_delete_trigger_leaves_no_audit_row(seeded, db_pool):
    with db_pool.connection() as conn:
        conn.execute("create table audit (event_id bigint, deleted_at timestamptz)")
        conn.execute(
            "create function audit_delete() returns trigger language plpgsql as $$"
            " begin insert into audit values (old.event_id, now()); return old; end $$"
        )
        conn.execute(
            "create trigger events_audit after delete on events"
            " for each row execute function audit_delete()"
        )
    a, b = _connect(seeded), _connect(seeded)
    try:
        deleted = delete_after(a, [_EVENTS], CutKey(datetime(2014, 1, 1), True, False))
        assert deleted["events"] > 0
        assert _count(a, "audit") == deleted["events"]  # the trigger ran inside A
        a.rollback()
        assert _count(a, "audit") == 0
        assert _count(b, "audit") == 0
    finally:
        a.close()
        b.close()


def test_harness_commit_guard_refuses_any_commit(seeded):
    a, b = _connect(seeded), _connect(seeded)
    try:
        total = _count(b, "events")
        b.rollback()
        leakage._arm_commit_guard(a)
        delete_after(a, [_EVENTS], CutKey(datetime(2014, 1, 1), True, False))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            a.commit()
        assert _count(b, "events") == total
        assert _count(a, "events") == total  # the failed commit rolled the cut back
    finally:
        a.rollback()
        a.close()
        b.close()


def test_harness_module_has_no_commit_call():
    source = inspect.getsource(leakage)
    assert ".commit(" not in source, (
        "triage.diagnostics.leakage must never commit: the check's cuts live in one"
        " transaction that always ends in ROLLBACK (ADR-0034)"
    )


# --------------------------------------------------------------------------------------
# plan — the cut plan (no database)
# --------------------------------------------------------------------------------------


def test_plan_cuts_are_strictly_descending_and_cover_every_check():
    plan = plan_cuts(_config())
    assert list(plan.as_of_dates) == AS_OF_DATES
    assert all(a > b for a, b in zip(plan.cuts, plan.cuts[1:]))
    assert len(set(plan.cuts)) == len(plan.cuts)
    assert {check.cut for check in plan.checks} == set(plan.cuts)
    assert sum(len(plan.checks_at(cut)) for cut in plan.cuts) == len(plan.checks)


def test_plan_equal_timestamps_put_keep_equal_first():
    """The labels of 2013-01-01 (6 months) are cut at 2013-07-01, the date of the next
    cohort and features cuts: cohort (keeps all equal), features (keeps entity equal),
    labels (keeps none) — each deleting a superset of the one before."""
    plan = plan_cuts(_config())
    at = datetime(2013, 7, 1)
    same = [cut for cut in plan.cuts if cut.at == at]
    assert [(c.keep_equal_entity, c.keep_equal_event) for c in same] == [
        (True, True),
        (True, False),
        (False, False),
    ]
    assert [check.component for cut in same for check in plan.checks_at(cut)] == [
        "cohort",
        "features",
        "labels",
    ]
    assert plan.checks_at(same[2])[0].as_of_date == date(2013, 1, 1)


def test_plan_boundaries_per_component():
    cohort, features, labels = (
        Check("cohort", date(2014, 1, 1)),
        Check("features", date(2014, 1, 1)),
        Check("labels", date(2014, 1, 1), "6 months"),
    )
    assert cohort.cut == CutKey(datetime(2014, 1, 1), True, True)
    assert features.cut.keeps_equal(_ENTITIES) and not features.cut.keeps_equal(_EVENTS)
    assert labels.cut == CutKey(datetime(2014, 7, 1), False, False)
    assert labels.cut.describe() == "rows known on or after 2014-07-01"


def test_plan_a_date_can_carry_both_label_timespans():
    temporal = {
        **{k: v for k, v in TEMPORAL_CONFIG.items() if k != "label_timespans"},
        "training_as_of_date_frequencies": "3 months",
        "test_as_of_date_frequencies": "3 months",
        "max_training_histories": "1 year",
        "training_label_timespans": "3 months",
        "test_label_timespans": "6 months",
    }
    plan = plan_cuts({"temporal_config": temporal})
    both = [d for d, spans in plan.label_timespans.items() if len(spans) == 2]
    assert date(2013, 7, 1) in both
    labels = [
        str(c.label_timespan)
        for c in plan.checks
        if c.component == "labels" and c.as_of_date == date(2013, 7, 1)
    ]
    assert sorted(labels) == ["3 months", "6 months"]


def test_plan_dates_restrict_and_unknown_dates_fail():
    plan = plan_cuts(_config(), [date(2014, 1, 1)])
    assert plan.as_of_dates == (date(2014, 1, 1),)
    assert {c.as_of_date for c in plan.checks} == {date(2014, 1, 1)}
    with pytest.raises(LeakageCheckError, match="2014-02-01: not an as_of_date"):
        plan_cuts(_config(), [date(2014, 2, 1)])


# --------------------------------------------------------------------------------------
# compare — the comparisons (no database)
# --------------------------------------------------------------------------------------

_KEYS = ("as_of_date", "entity_id")
_DAY = date(2014, 1, 1)


def _frame(**columns: list[Any]) -> pl.DataFrame:
    n = len(next(iter(columns.values())))
    return pl.DataFrame(
        {"as_of_date": [_DAY] * n, "entity_id": list(range(1, n + 1)), **columns}
    )


def test_compare_features_is_nan_aware_and_tolerant():
    baseline = _frame(
        a=[1.0, None, float("nan"), 3.0],
        b=[0.1 + 0.2, 1e-13, 5.0, 7.0],
        c=["x", None, "y", "z"],
        **{"a__missing": [0, 1, 1, 0]},
    )
    observed = _frame(
        a=[1.0, None, float("nan"), 3.0],
        b=[0.3, 0.0, 5.0 * (1 + 1e-12), 7.0],  # inside rtol 1e-9 / atol 1e-12
        c=["x", None, "y", "z"],
        **{"a__missing": [0, 1, 1, 1]},  # flags are skipped
    )
    check = Check("features", _DAY)
    assert compare_features(check, baseline, observed, _KEYS) == []


def test_compare_features_names_columns_and_rows():
    baseline = _frame(a=[1.0, 2.0, None], b=[1, 1, 1], c=["x", "y", "z"])
    observed = _frame(a=[1.0, 2.5, 0.0], b=[1, 1, 2], c=["x", "y", "z"]).filter(
        pl.col("entity_id") != 1
    )
    [finding] = compare_features(Check("features", _DAY), baseline, observed, _KEYS)
    assert finding.component == "features"
    assert finding.removed == 1  # entity 1 lost its row
    assert finding.changed == 2  # entities 2 and 3
    assert dict(finding.columns) == {"a": 2, "b": 1}
    assert finding.columns[0] == ("a", 2)  # most-changed first
    assert finding.examples == (1, 2, 3)
    assert finding.cut.startswith("event rows known on or after 2014-01-01")


def test_compare_cohort_and_labels():
    check = Check("cohort", _DAY)
    assert compare_cohort(check, {1, 2}, {1, 2}) == []
    [cohort] = compare_cohort(check, {1, 2, 3}, {2, 3, 4})
    assert (cohort.added, cohort.removed, cohort.examples) == (1, 1, (1, 4))

    lcheck = Check("labels", _DAY, "6 months")
    same = {1: ((1,),), 2: ((0.1 + 0.2,),)}
    assert compare_labels(lcheck, same, {1: ((1,),), 2: ((0.3,),)}) == []
    [labels] = compare_labels(lcheck, same, {2: ((1.0,),), 3: ((0,),)})
    assert (labels.added, labels.removed, labels.changed) == (1, 1, 1)
    assert labels.label_timespan == "6 months"


# --------------------------------------------------------------------------------------
# discovery and fingerprint (database)
# --------------------------------------------------------------------------------------


def _discover(db_url: str, config: dict[str, Any]):
    plan = plan_cuts(config, [date(2014, 1, 1)])
    experiment = leakage._Experiment.from_config(config)
    sources = leakage.declared_sources(config)
    conn = _connect(db_url)
    try:
        return discover_relations(conn, plan, experiment, sources)
    finally:
        conn.rollback()
        conn.close()


def test_discovery_reports_an_undeclared_table(seeded):
    not_checked = _discover(seeded, _config(cohort=UNDECLARED_COHORT))
    assert [n.relation for n in not_checked] == ["public.flags"]
    assert "cohort query" in not_checked[0].reason
    assert "not declared" in not_checked[0].reason


def test_discovery_sees_through_a_view_over_a_declared_source(seeded, db_pool):
    with db_pool.connection() as conn:
        conn.execute("create view active_entities as select * from entities")
    cohort = (
        "select entity_id from active_entities where start_time <= {as_of_date}::date"
    )
    assert _discover(seeded, _config(cohort=cohort)) == []


def test_fingerprint_equal_before_and_after_a_run_that_found_leaks(seeded):
    config = _config(labels=LEAKY_LABELS)
    sources = leakage.declared_sources(config)
    before = fingerprints(seeded, sources)
    report = check_leakage(seeded, config, [date(2013, 7, 1)])
    assert report.leakage_found
    assert fingerprints(seeded, sources) == before
    assert before["events"]["row_count"] > 0


# --------------------------------------------------------------------------------------
# cli — the command on a clean experiment and one planted leak per component
# --------------------------------------------------------------------------------------


def _run_cli(
    tmp_path: Path, monkeypatch, db_url: str, config: dict[str, Any], *extra: str
) -> tuple[Any, dict[str, Any]]:
    """Run ``triage check leakage`` on ``config``; return the result and the JSON report
    (empty when the check wrote none)."""
    monkeypatch.chdir(tmp_path)  # no database.yaml / .env shadows DATABASE_URL
    config_path = tmp_path / "experiment.yaml"
    config_path.write_text(yaml.safe_dump(config))
    report_path = tmp_path / "report.json"
    result = runner.invoke(
        cli.app,
        ["check", "leakage", str(config_path), "--json", str(report_path), *extra],
        env={"DATABASE_URL": db_url},
    )
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    return result, report


def _components(report: dict[str, Any]) -> set[str]:
    return {finding["component"] for finding in report["findings"]}


def test_cli_clean_experiment_exits_0(seeded, tmp_path, monkeypatch):
    result, report = _run_cli(tmp_path, monkeypatch, seeded, _config())
    assert result.exit_code == 0, result.output
    assert report["findings"] == []
    assert report["dates_checked"] == [d.isoformat() for d in AS_OF_DATES]
    assert report["not_checked"] == []
    assert "no differences" in result.output


def test_cli_label_window_without_upper_bound_exits_1(seeded, tmp_path, monkeypatch):
    result, report = _run_cli(
        tmp_path, monkeypatch, seeded, _config(labels=LEAKY_LABELS)
    )
    assert result.exit_code == 1, result.output
    assert _components(report) == {"labels"}
    assert all(f["label_timespan"] == LABEL_TIMESPAN for f in report["findings"])


def test_cli_cohort_selected_by_future_activity_exits_1(seeded, tmp_path, monkeypatch):
    result, report = _run_cli(
        tmp_path, monkeypatch, seeded, _config(cohort=LEAKY_COHORT)
    )
    assert result.exit_code == 1, result.output
    assert "cohort" in _components(report)
    cohort = [f for f in report["findings"] if f["component"] == "cohort"]
    assert all(f["removed"] > 0 for f in cohort)


def test_cli_late_arriving_data_names_the_feature_columns(
    seeded, tmp_path, monkeypatch
):
    """The Source declares known_at (occurred_at + 30 days); featurizer reads occurred_at."""
    result, report = _run_cli(
        tmp_path, monkeypatch, seeded, _config(events_knowledge_date="known_at")
    )
    assert result.exit_code == 1, result.output
    features = [f for f in report["findings"] if f["component"] == "features"]
    assert features, report["findings"]
    columns = {name for f in features for name in f["columns"]}
    # Every moved column reads the events path, the one whose knowledge date is late.
    assert columns and all("(events." in name for name in columns), columns
    assert {"SUM(events.amount)", "SUM(events.amount|interval=P90D)"} <= columns
    assert not any("size" in name for name in columns)  # the entity's own attribute


def test_cli_undeclared_table_exits_0_and_is_listed(seeded, tmp_path, monkeypatch):
    result, report = _run_cli(
        tmp_path, monkeypatch, seeded, _config(cohort=UNDECLARED_COHORT)
    )
    assert result.exit_code == 0, result.output
    assert [n["relation"] for n in report["not_checked"]] == ["public.flags"]
    assert "public.flags" in result.output


def test_cli_role_without_delete_exits_2(seeded, db_pool, tmp_path, monkeypatch):
    role = f"leak_reader_{uuid.uuid4().hex[:8]}"
    with db_pool.connection() as conn:
        conn.execute(f"create role {role} login")
        conn.execute(f"grant select on entities, events, flags to {role}")
    reader_url = re.sub(r"://[^@]+@", f"://{role}@", seeded)
    result, report = _run_cli(tmp_path, monkeypatch, reader_url, _config())
    assert result.exit_code == 2, result.output
    assert report == {}
    text = " ".join(result.output.split())
    assert "no DELETE privilege" in text and "never commits" in text


def test_cli_unknown_date_exits_2(seeded, tmp_path, monkeypatch):
    result, _ = _run_cli(
        tmp_path, monkeypatch, seeded, _config(), "--dates", "2014-02-01"
    )
    assert result.exit_code == 2, result.output
    assert "not an as_of_date" in " ".join(result.output.split())


def test_cli_help_says_the_check_never_commits():
    result = runner.invoke(cli.app, ["check", "leakage", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(re.sub(r"[│╭╮╰╯─]", " ", result.output).split())
    assert "never commits" in text
    assert "Exit codes" in text


# --------------------------------------------------------------------------------------
# review additions
# --------------------------------------------------------------------------------------


def test_discovery_reports_an_undeclared_graph_edge_table(seeded, db_pool):
    """featurizer also reads ``graph_relationships[].edges.table``, not only entity tables."""
    with db_pool.connection() as conn:
        conn.execute("create table contacts (src bigint, dst bigint, ts date)")
    config = _config()
    config["feature_config"]["graph_relationships"] = [
        {
            "name": "contacts",
            "left": "entities",
            "edges": {
                "table": "contacts",
                "source": "src",
                "target": "dst",
                "timestamp": "ts",
            },
        }
    ]
    not_checked = _discover(seeded, config)
    assert [n.relation for n in not_checked] == ["public.contacts"]
    assert "featurizer config" in not_checked[0].reason


def test_harness_delete_boundary_at_an_equal_timestamp(seeded, db_pool):
    """A row known exactly at the cut: kept by cohort cuts, kept in entity Sources by
    feature cuts, deleted from event Sources by feature cuts, deleted by label cuts."""
    at = datetime(2014, 1, 1)
    with db_pool.connection() as conn:
        conn.execute("insert into entities values (99, date '2014-01-01', 1, 'e99')")
        conn.execute(
            "insert into events values (9999, 99, date '2014-01-01', date '2014-01-01', 1, 0)"
        )

    def surviving(cut: CutKey) -> tuple[bool, bool]:
        conn = _connect(seeded)
        try:
            delete_after(conn, [_EVENTS, _ENTITIES], cut)
            ent = conn.execute("select count(*) from entities where entity_id = 99")
            evt = conn.execute("select count(*) from events where event_id = 9999")
            return (ent.fetchone() == (1,), evt.fetchone() == (1,))
        finally:
            conn.rollback()
            conn.close()

    assert surviving(Check("cohort", at.date()).cut) == (True, True)
    assert surviving(Check("features", at.date()).cut) == (True, False)
    assert surviving(CutKey(at, False, False)) == (False, False)
