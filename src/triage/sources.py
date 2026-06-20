"""Source registry: declared input tables and their version pins.

Implements ADR-0014 (see docs/derivation-dag.md §3). Sources are explicitly
declared input tables; each data load bumps a version pin in
``triage.source_versions``. At plan time :func:`resolve_pins` freezes the
current pin per declared source — an unpinned source resolves to ``None``
(volatile: never a cache hit) with a loud warning, never an error.

Fingerprints (row count + max knowledge date) are advisory drift detection
only; they never enter artifact identity.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Iterable
from typing import Any

from psycopg_pool import ConnectionPool

from triage.logging import get_logger

logger = get_logger(__name__)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _quote_relation(relation: str) -> str:
    """Quote a (possibly schema-qualified) relation name for interpolation.

    Registry values are user-declared config, but they still get validated and
    quoted before entering SQL text — fail fast on anything that is not a
    plain identifier path.
    """
    parts = relation.split(".")
    if len(parts) > 2 or not all(_IDENTIFIER.match(part) for part in parts):
        raise ValueError(
            f"Relation {relation!r} is not a valid (schema-qualified) identifier;"
            + " expected e.g. 'semantic.events'"
        )
    return ".".join(f'"{part}"' for part in parts)


def _quote_column(column: str) -> str:
    if not _IDENTIFIER.match(column):
        raise ValueError(f"Column {column!r} is not a valid identifier")
    return f'"{column}"'


def register_source(
    pool: ConnectionPool,
    source_name: str,
    relation: str,
    knowledge_date_column: str | None = None,
    description: str | None = None,
) -> None:
    """Declare a source (idempotent upsert on the name)."""
    _ = _quote_relation(relation)  # validate early, before anything is stored
    if knowledge_date_column is not None:
        _ = _quote_column(knowledge_date_column)
    with pool.connection() as conn:
        conn.execute(
            """
                insert into triage.sources
                    (source_name, relation, knowledge_date_column, description)
                values (%(name)s, %(relation)s, %(kdc)s, %(description)s)
                on conflict (source_name) do update
                    set relation = excluded.relation,
                        knowledge_date_column = excluded.knowledge_date_column,
                        description = excluded.description
                """,
            {
                "name": source_name,
                "relation": relation,
                "kdc": knowledge_date_column,
                "description": description,
            },
        )
    logger.info(f"Registered source {source_name!r} -> {relation}")


def get_source(pool: ConnectionPool, source_name: str) -> dict[str, Any] | None:
    with pool.connection() as conn:
        row = conn.execute(
            "select * from triage.sources where source_name = %(name)s",
            {"name": source_name},
        ).fetchone()
    return dict(row) if row else None


def list_sources(pool: ConnectionPool) -> list[dict[str, Any]]:
    """All sources with their current pin (null version_label = unpinned)."""
    with pool.connection() as conn:
        rows = conn.execute("""
                select s.source_name, s.relation, s.knowledge_date_column,
                       s.description, p.version_label, p.registered_at
                from triage.sources s
                left join triage.current_source_pins p using (source_name)
                order by s.source_name
                """).fetchall()
    return [dict(row) for row in rows]


def capture_fingerprint(pool: ConnectionPool, source_name: str) -> dict[str, Any]:
    """Cheap advisory fingerprint: row count + max knowledge date (if declared)."""
    source = get_source(pool, source_name)
    if source is None:
        raise ValueError(
            f"Source {source_name!r} is not registered; register it with"
            + " `triage source register` before fingerprinting"
        )
    relation = _quote_relation(source["relation"])
    kdc = source["knowledge_date_column"]
    max_expr = f"max({_quote_column(kdc)})::text" if kdc else "null"
    with pool.connection() as conn:
        row = conn.execute(
            f"select count(*) as row_count, {max_expr} as max_knowledge_date from {relation}"
        ).fetchone()
    return {
        "row_count": row["row_count"],
        "max_knowledge_date": row["max_knowledge_date"],
    }


def bump_source(
    pool: ConnectionPool, source_name: str, version_label: str | None = None
) -> str:
    """Record a new version pin for a source, fingerprinting it now.

    Returns the version label (generated from the current UTC time when not
    given). Intended callers: the ETL/loader on load completion, or
    ``triage source bump`` for manual loads.
    """
    fingerprint = capture_fingerprint(pool, source_name)  # also validates existence
    if version_label is None:
        version_label = datetime.datetime.now(datetime.timezone.utc).strftime(
            "v%Y%m%d-%H%M%S"
        )
    with pool.connection() as conn:
        conn.execute(
            """
                insert into triage.source_versions
                    (source_name, version_label, fingerprint)
                values (%(name)s, %(label)s, cast(%(fingerprint)s as jsonb))
                """,
            {
                "name": source_name,
                "label": version_label,
                "fingerprint": json.dumps(fingerprint),
            },
        )
    logger.info(
        f"Pinned source {source_name!r} at {version_label!r} (fingerprint {fingerprint})"
    )
    return version_label


def current_pin(pool: ConnectionPool, source_name: str) -> dict[str, Any] | None:
    with pool.connection() as conn:
        row = conn.execute(
            "select * from triage.current_source_pins where source_name = %(name)s",
            {"name": source_name},
        ).fetchone()
    return dict(row) if row else None


def resolve_pins(
    pool: ConnectionPool, declared: Iterable[str]
) -> dict[str, str | None]:
    """Freeze the current pin of every declared source (plan time, ADR-0014).

    Unregistered or unpinned sources resolve to ``None`` — volatile, meaning
    every derivation touching them is rebuilt instead of cache-hit — and emit
    a loud warning. Never raises for missing pins: the failure mode is a
    wasted rebuild, not a blocked run.
    """
    pins: dict[str, str | None] = {}
    for name in declared:
        source = get_source(pool, name)
        if source is None:
            logger.warning(
                f"Declared source {name!r} is NOT registered — treating it as"
                + " volatile (no cache reuse downstream). Register it with"
                + " `triage source register` and pin it with `triage source bump`."
            )
            pins[name] = None
            continue
        pin = current_pin(pool, name)
        if pin is None:
            logger.warning(
                f"Source {name!r} has no version pin — treating it as volatile"
                + " (no cache reuse downstream). Pin it with `triage source bump"
                + f" {name}` after each data load."
            )
            pins[name] = None
        else:
            pins[name] = pin["version_label"]
    return pins


def check_drift(pool: ConnectionPool, source_name: str) -> bool:
    """Advisory drift check: did the data move while the pin stayed put?

    Compares the current fingerprint against the one stored with the source's
    current pin. Returns True (and warns loudly) on drift; False when there is
    nothing to compare (no pin or no stored fingerprint) or no drift.
    """
    pin = current_pin(pool, source_name)
    if pin is None or pin["fingerprint"] is None:
        return False
    now = capture_fingerprint(pool, source_name)
    pinned = pin["fingerprint"]
    if now != pinned:
        logger.warning(
            f"Source {source_name!r} drifted since pin {pin['version_label']!r}:"
            + f" fingerprint was {pinned}, is now {now}. The data changed but the"
            + f" pin did not — run `triage source bump {source_name}` if a new"
            + " load landed."
        )
        return True
    return False


def record_run_pins(
    pool: ConnectionPool, run_id: str, pins: dict[str, str | None]
) -> None:
    """Persist the pins frozen for a run (the ``guix describe`` analog).

    Captures a build-time fingerprint per registered source so later drift
    analysis can compare against what the run actually saw.
    """
    with pool.connection() as conn:
        for name, version_label in pins.items():
            fingerprint = (
                capture_fingerprint(pool, name)
                if get_source(pool, name) is not None
                else None
            )
            conn.execute(
                """
                    insert into triage.run_source_pins
                        (run_id, source_name, version_label, fingerprint)
                    values (%(run_id)s, %(name)s, %(label)s, cast(%(fingerprint)s as jsonb))
                    """,
                {
                    "run_id": run_id,
                    "name": name,
                    "label": version_label,
                    "fingerprint": json.dumps(fingerprint) if fingerprint else None,
                },
            )
