"""Tests for the artifact DAG store (ADR-0013, ADR-0015) on the greenfield schema."""

import json

import pytest

from triage.artifacts import (
    begin_artifact,
    cache_hit,
    closure,
    dependents,
    get_artifact,
    mark_built,
    mark_failed,
)
from triage.component.results_schema import upgrade_db
from triage.derivation import as_uuid, derive


@pytest.fixture
def triage_db(db_url, db_pool):
    """Apply the greenfield baseline migration."""
    upgrade_db(dburl=db_url)
    return db_pool


def build(pool, derivation, kind, config, parents=(), **kwargs):
    begin_artifact(pool, derivation, kind, config, parents=parents, **kwargs)
    mark_built(pool, derivation.id)


def _seed_run(pool):
    """An experiment + run row so ``built_by_run`` FKs resolve; returns run_id (str)."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-notify', '{}'::jsonb, 'classification')"
            " on conflict do nothing"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status)"
            " values ('exp-notify', 'local', 'started') returning run_id"
        ).fetchone()["run_id"]
    return str(run_id)


def listen(pool):
    """Open a dedicated connection LISTENing on ``run_progress`` (read-dashboard-spec §4).

    The LISTEN must be committed before the producing action so the channel
    registration is live; the returned connection stays checked out of the pool
    for the test's lifetime and is drained with :func:`collect_notifies`.
    """
    conn = pool.getconn()
    conn.execute("listen run_progress")
    conn.commit()
    return conn


def collect_notifies(conn, *, expected, timeout=5.0):
    """Drain at least ``expected`` ``run_progress`` payloads as parsed dicts.

    Bounded poll (psycopg3 ``notifies(timeout=, stop_after=)``) so a missing
    NOTIFY fails as a timeout, never an indefinite hang.
    """
    return [
        json.loads(note.payload)
        for note in conn.notifies(timeout=timeout, stop_after=expected)
    ]


def test_begin_then_built_then_cache_hit(triage_db):
    derivation = derive("cohort", {"q": "select 1"}, source_pins={"events": "v1"})

    assert cache_hit(triage_db, derivation) is None  # nothing recorded yet

    row = begin_artifact(
        triage_db,
        derivation,
        "cohort",
        {"q": "select 1"},
        source_pins={"events": "v1"},
    )
    assert row["status"] == "building"
    assert cache_hit(triage_db, derivation) is None  # building != built

    mark_built(triage_db, derivation.id, output_ref="triage.cohorts@2026-01-01")
    hit = cache_hit(triage_db, derivation)
    assert hit is not None
    assert hit["output_ref"] == "triage.cohorts@2026-01-01"


def test_volatile_artifacts_never_cache_hit(triage_db):
    derivation = derive("cohort", {"q": "select 1"}, source_pins={"events": None})
    assert not derivation.cacheable

    build(triage_db, derivation, "cohort", {"q": "select 1"})
    artifact = get_artifact(triage_db, derivation.id)
    assert artifact is not None  # provenance is still recorded
    assert artifact["cacheable"] is False
    assert cache_hit(triage_db, derivation) is None  # but never reused


def test_rebuild_resets_status(triage_db):
    derivation = derive("cohort", {"q": "select 1"}, source_pins={"events": None})
    build(triage_db, derivation, "cohort", {"q": "select 1"})

    row = begin_artifact(triage_db, derivation, "cohort", {"q": "select 1"})
    assert row["status"] == "building"
    assert row["built_at"] is None


def test_failed_artifacts_do_not_hit(triage_db):
    derivation = derive("model", {"class": "DT"}, source_pins={"events": "v1"})
    begin_artifact(triage_db, derivation, "model", {"class": "DT"})
    mark_failed(triage_db, derivation.id)
    assert cache_hit(triage_db, derivation) is None


def test_marking_unknown_artifact_fails_fast(triage_db):
    with pytest.raises(ValueError, match="no such artifact"):
        mark_built(triage_db, "deadbeef")
    with pytest.raises(ValueError, match="no such artifact"):
        mark_failed(triage_db, "deadbeef")


def test_closure_and_dependents_walk_the_dag(triage_db):
    pins = {"events": "v1"}
    cohort = derive("cohort", {"q": "c"}, source_pins=pins)
    labels = derive("labels", {"q": "l"}, source_pins=pins)
    group = derive("feature_group", {"g": "demographics"}, source_pins=pins)
    matrix = derive("matrix", {"split": "train"}, parents=[cohort, labels, group])
    model = derive("model", {"class": "DT"}, parents=[matrix])

    build(triage_db, cohort, "cohort", {"q": "c"})
    build(triage_db, labels, "labels", {"q": "l"})
    build(triage_db, group, "feature_group", {"g": "demographics"})
    build(
        triage_db,
        matrix,
        "matrix",
        {"split": "train"},
        parents=[cohort.id, labels.id, group.id],
    )
    build(triage_db, model, "model", {"class": "DT"}, parents=[matrix.id])

    up = closure(triage_db, model.id)
    assert {row["artifact_id"] for row in up} == {
        model.id,
        matrix.id,
        cohort.id,
        labels.id,
        group.id,
    }
    depths = {row["artifact_id"]: row["depth"] for row in up}
    assert depths[model.id] == 0
    assert depths[matrix.id] == 1
    assert depths[cohort.id] == 2

    down = dependents(triage_db, cohort.id)
    assert {row["artifact_id"] for row in down} == {cohort.id, matrix.id, model.id}


def test_edge_insertion_is_idempotent(triage_db):
    parent = derive("cohort", {"q": "c"}, source_pins={"events": "v1"})
    child = derive("matrix", {"m": 1}, parents=[parent])
    build(triage_db, parent, "cohort", {"q": "c"})
    build(triage_db, child, "matrix", {"m": 1}, parents=[parent.id])
    # volatile-style re-run records the same edge again without erroring
    begin_artifact(triage_db, child, "matrix", {"m": 1}, parents=[parent.id])

    with triage_db.connection() as conn:
        count = conn.execute(
            "select count(*) as n from triage.artifact_inputs where artifact_id = %(id)s",
            {"id": child.id},
        ).fetchone()["n"]
    assert count == 1


def test_as_uuid_is_deterministic_and_distinct():
    one = derive("matrix", {"m": 1})
    other = derive("matrix", {"m": 2})
    assert as_uuid(one.id) == as_uuid(one.id)
    assert as_uuid(one.id) != as_uuid(other.id)


@pytest.fixture
def warning_messages():
    """Collect loguru WARNING+ records emitted during a test."""
    from loguru import logger as loguru_logger

    messages = []
    sink_id = loguru_logger.add(
        lambda message: messages.append(str(message)), level="WARNING"
    )
    yield messages
    loguru_logger.remove(sink_id)


def test_logical_fallback_on_engine_drift(triage_db, warning_messages):
    pins = {"events": "v1"}
    old = derive(
        "model",
        {"c": "DT"},
        source_pins=pins,
        engine_versions={"scikit-learn": "1.5.1"},
    )
    build(
        triage_db,
        old,
        "model",
        {"c": "DT"},
        source_pins=pins,
        engine_versions={"scikit-learn": "1.5.1"},
    )
    new = derive(
        "model",
        {"c": "DT"},
        source_pins=pins,
        engine_versions={"scikit-learn": "1.5.2"},
    )

    assert cache_hit(triage_db, new) is None  # strict default: drift = miss
    hit = cache_hit(triage_db, new, policy="logical")
    assert hit is not None
    assert hit["artifact_id"] == old.id
    assert any("ENGINE-DRIFT REUSE" in message for message in warning_messages)


def test_logical_fallback_never_returns_volatile_or_unbuilt(triage_db):
    volatile = derive(
        "model",
        {"c": "DT"},
        source_pins={"events": None},
        engine_versions={"scikit-learn": "1.5.1"},
    )
    build(
        triage_db,
        volatile,
        "model",
        {"c": "DT"},
        source_pins={"events": None},
        engine_versions={"scikit-learn": "1.5.1"},
    )
    drifted_volatile = derive(
        "model",
        {"c": "DT"},
        source_pins={"events": None},
        engine_versions={"scikit-learn": "1.5.2"},
    )
    # volatile derivations skip lookup entirely, fallback included
    assert cache_hit(triage_db, drifted_volatile, policy="logical") is None

    pins = {"events": "v1"}
    failed = derive(
        "model",
        {"c": "RF"},
        source_pins=pins,
        engine_versions={"scikit-learn": "1.5.1"},
    )
    begin_artifact(
        triage_db,
        failed,
        "model",
        {"c": "RF"},
        source_pins=pins,
        engine_versions={"scikit-learn": "1.5.1"},
    )
    mark_failed(triage_db, failed.id)
    drifted = derive(
        "model",
        {"c": "RF"},
        source_pins=pins,
        engine_versions={"scikit-learn": "1.5.2"},
    )
    assert cache_hit(triage_db, drifted, policy="logical") is None


def test_unknown_cache_policy_fails_fast(triage_db):
    derivation = derive("cohort", {"q": 1}, source_pins={"events": "v1"})
    with pytest.raises(ValueError, match="policy"):
        cache_hit(triage_db, derivation, policy="yolo")


# ---- live telemetry: pg_notify('run_progress') (read-dashboard-spec §4) ------


def test_begin_artifact_emits_building(triage_db):
    run_id = _seed_run(triage_db)
    listener = listen(triage_db)
    try:
        derivation = derive("cohort", {"q": "c"}, source_pins={"events": "v1"})
        begin_artifact(
            triage_db,
            derivation,
            "cohort",
            {"q": "c"},
            source_pins={"events": "v1"},
            run_id=run_id,
        )
        payloads = collect_notifies(listener, expected=1)
    finally:
        triage_db.putconn(listener)

    assert payloads == [{"run_id": run_id, "kind": "cohort", "status": "building"}]


def test_mark_built_emits_built_with_kind_and_run_id(triage_db):
    run_id = _seed_run(triage_db)
    listener = listen(triage_db)
    try:
        derivation = derive("model", {"c": "DT"}, source_pins={"events": "v1"})
        begin_artifact(
            triage_db,
            derivation,
            "model",
            {"c": "DT"},
            source_pins={"events": "v1"},
            run_id=run_id,
        )
        mark_built(triage_db, derivation.id, kind="model", run_id=run_id)
        # begin -> 'building', mark_built -> 'built'
        payloads = collect_notifies(listener, expected=2)
    finally:
        triage_db.putconn(listener)

    assert {"run_id": run_id, "kind": "model", "status": "building"} in payloads
    assert {"run_id": run_id, "kind": "model", "status": "built"} in payloads


def test_mark_failed_emits_failed_with_kind_and_run_id(triage_db):
    run_id = _seed_run(triage_db)
    derivation = derive("matrix", {"split": "train"}, source_pins={"events": "v1"})
    begin_artifact(
        triage_db,
        derivation,
        "matrix",
        {"split": "train"},
        source_pins={"events": "v1"},
        run_id=run_id,
    )
    listener = listen(triage_db)
    try:
        mark_failed(triage_db, derivation.id, kind="matrix", run_id=run_id)
        payloads = collect_notifies(listener, expected=1)
    finally:
        triage_db.putconn(listener)

    assert payloads == [{"run_id": run_id, "kind": "matrix", "status": "failed"}]


def test_no_emit_without_run_id_or_kind(triage_db):
    """begin/mark with no run_id (or mark_built without kind) must not error and
    must emit nothing — the headless-safe, backward-compatible path."""
    listener = listen(triage_db)
    try:
        derivation = derive("cohort", {"q": "c"}, source_pins={"events": "v1"})
        # run_id=None on begin -> no NOTIFY
        begin_artifact(triage_db, derivation, "cohort", {"q": "c"})
        # mark_built without kind/run_id -> no NOTIFY (backward compatible)
        mark_built(triage_db, derivation.id)
        payloads = collect_notifies(listener, expected=1, timeout=1.0)
    finally:
        triage_db.putconn(listener)

    assert payloads == []  # timed out with nothing — no error, no emit


def test_notify_is_a_no_op_without_listener(triage_db):
    """A NOTIFY with no listener must not error — the headless core runs unchanged."""
    run_id = _seed_run(triage_db)
    derivation = derive("labels", {"q": "l"}, source_pins={"events": "v1"})
    # nobody is LISTENing; these must complete cleanly
    begin_artifact(
        triage_db,
        derivation,
        "labels",
        {"q": "l"},
        source_pins={"events": "v1"},
        run_id=run_id,
    )
    mark_built(triage_db, derivation.id, kind="labels", run_id=run_id)
    assert get_artifact(triage_db, derivation.id)["status"] == "built"
