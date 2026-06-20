"""Tests for the source registry + pins (ADR-0014) on the greenfield schema."""

import pytest
from loguru import logger as loguru_logger

from triage.component.results_schema import upgrade_db
from triage.sources import (
    bump_source,
    capture_fingerprint,
    check_drift,
    current_pin,
    get_source,
    list_sources,
    record_run_pins,
    register_source,
    resolve_pins,
)


@pytest.fixture
def triage_db(db_url, db_pool):
    """Apply the greenfield baseline migration and seed a raw source table."""
    upgrade_db(dburl=db_url)
    with db_pool.connection() as conn:
        conn.execute("create schema raw")
        conn.execute("create table raw.events (event_id int, knowledge_date date)")
        conn.execute("""
                insert into raw.events values
                    (1, '2026-01-15'), (2, '2026-02-20'), (3, '2026-03-25')
                """)
    return db_pool


@pytest.fixture
def warning_messages():
    """Collect loguru WARNING+ records emitted during a test."""
    messages = []
    sink_id = loguru_logger.add(
        lambda message: messages.append(str(message)), level="WARNING"
    )
    yield messages
    loguru_logger.remove(sink_id)


def register_events(pool):
    register_source(
        pool,
        "events",
        "raw.events",
        knowledge_date_column="knowledge_date",
        description="raw event stream",
    )


def test_register_and_get_roundtrip(triage_db):
    register_events(triage_db)
    source = get_source(triage_db, "events")
    assert source is not None
    assert source["relation"] == "raw.events"
    assert source["knowledge_date_column"] == "knowledge_date"

    # idempotent upsert: re-register updates in place
    register_source(triage_db, "events", "raw.events", description="updated")
    source = get_source(triage_db, "events")
    assert source is not None
    assert source["description"] == "updated"
    assert len(list_sources(triage_db)) == 1


def test_register_rejects_invalid_relation(triage_db):
    with pytest.raises(ValueError, match="not a valid"):
        register_source(triage_db, "evil", "raw.events; drop table users")


def test_fingerprint_captures_count_and_max_knowledge_date(triage_db):
    register_events(triage_db)
    fingerprint = capture_fingerprint(triage_db, "events")
    assert fingerprint == {"row_count": 3, "max_knowledge_date": "2026-03-25"}


def test_fingerprint_requires_registration(triage_db):
    with pytest.raises(ValueError, match="not registered"):
        capture_fingerprint(triage_db, "ghost")


def test_bump_pins_with_fingerprint(triage_db):
    register_events(triage_db)
    label = bump_source(triage_db, "events", "v1")
    assert label == "v1"
    pin = current_pin(triage_db, "events")
    assert pin is not None
    assert pin["version_label"] == "v1"
    assert pin["fingerprint"]["row_count"] == 3


def test_bump_generates_label_when_missing(triage_db):
    register_events(triage_db)
    label = bump_source(triage_db, "events")
    assert label.startswith("v20")


def test_latest_bump_wins(triage_db):
    register_events(triage_db)
    bump_source(triage_db, "events", "v1")
    bump_source(triage_db, "events", "v2")
    pin = current_pin(triage_db, "events")
    assert pin is not None
    assert pin["version_label"] == "v2"


def test_resolve_pins_frozen_for_pinned_source(triage_db, warning_messages):
    register_events(triage_db)
    bump_source(triage_db, "events", "v1")
    pins = resolve_pins(triage_db, ["events"])
    assert pins == {"events": "v1"}
    assert not warning_messages


def test_resolve_pins_unpinned_is_volatile_and_warns(triage_db, warning_messages):
    register_events(triage_db)
    pins = resolve_pins(triage_db, ["events"])
    assert pins == {"events": None}
    assert any("volatile" in message for message in warning_messages)


def test_resolve_pins_unregistered_is_volatile_and_warns(triage_db, warning_messages):
    pins = resolve_pins(triage_db, ["ghost"])
    assert pins == {"ghost": None}
    assert any("NOT registered" in message for message in warning_messages)


def test_drift_detected_when_data_moves_without_bump(triage_db, warning_messages):
    register_events(triage_db)
    bump_source(triage_db, "events", "v1")
    assert check_drift(triage_db, "events") is False

    with triage_db.connection() as conn:
        conn.execute("insert into raw.events values (4, '2026-04-30')")
    assert check_drift(triage_db, "events") is True
    assert any("drifted" in message for message in warning_messages)


def test_drift_is_noop_without_pin(triage_db):
    register_events(triage_db)
    assert check_drift(triage_db, "events") is False


def test_record_run_pins_persists_the_frozen_set(triage_db):
    register_events(triage_db)
    bump_source(triage_db, "events", "v1")
    with triage_db.connection() as conn:
        run_id = conn.execute(
            "insert into triage.runs (profile) values ('local') returning run_id"
        ).fetchone()["run_id"]

    pins = resolve_pins(triage_db, ["events", "ghost"])
    record_run_pins(triage_db, str(run_id), pins)

    with triage_db.connection() as conn:
        rows = conn.execute(
            """
                select source_name, version_label, fingerprint
                from triage.run_source_pins
                where run_id = %(run_id)s
                order by source_name
                """,
            {"run_id": run_id},
        ).fetchall()
    assert [row["source_name"] for row in rows] == ["events", "ghost"]
    assert rows[0]["version_label"] == "v1"
    assert rows[0]["fingerprint"]["row_count"] == 3
    assert rows[1]["version_label"] is None  # volatile, recorded as such
    assert rows[1]["fingerprint"] is None  # unregistered: nothing to fingerprint
