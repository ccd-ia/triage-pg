"""Artifact DAG store: lookup-or-create over ``triage.artifacts``.

Implements ADR-0013/ADR-0015 (see docs/derivation-dag.md §2, §4). Build flow:

    derivation = derive(kind, config, parents, source_pins, engine_versions)
    if (hit := cache_hit(engine, derivation)) is not None:
        return hit                      # reuse, skip the build
    begin_artifact(engine, derivation, ...)
    try:
        ... build the thing ...
        mark_built(engine, derivation.id, output_ref=...)
    except:
        mark_failed(engine, derivation.id)
        raise

Volatile derivations (unpinned sources, ADR-0014) are recorded like any other
artifact — provenance still matters — but :func:`cache_hit` never returns them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from triage.derivation import Derivation, canonical_json
from triage.logging import get_logger

logger = get_logger(__name__)


def get_artifact(engine: Engine, artifact_id: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text("select * from triage.artifacts where artifact_id = :id"),
                {"id": artifact_id},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def cache_hit(engine: Engine, derivation: Derivation) -> dict[str, Any] | None:
    """Return the built artifact row for a derivation, or None to build.

    Volatile derivations never hit (their inputs are unpinned — the recorded
    output may be stale); neither do rows still building or failed.
    """
    if not derivation.cacheable:
        logger.info(
            f"Derivation {derivation.id[:12]}… is volatile (unpinned inputs)"
            + " — skipping cache lookup, rebuilding"
        )
        return None
    artifact = get_artifact(engine, derivation.id)
    if artifact is not None and artifact["status"] == "built":
        return artifact
    return None


def begin_artifact(
    engine: Engine,
    derivation: Derivation,
    kind: str,
    config: Mapping[str, Any],
    source_pins: Mapping[str, str | None] | None = None,
    engine_versions: Mapping[str, str] | None = None,
    run_id: str | None = None,
    parents: Sequence[str] = (),
) -> dict[str, Any]:
    """Upsert the artifact row as 'building' and record its input edges.

    Re-running an existing id (a volatile rebuild, or a retry after failure)
    resets it to 'building'. Parent artifacts must already exist — builds run
    bottom-up.
    """
    with engine.begin() as conn:
        row = (
            conn.execute(
                text("""
                    insert into triage.artifacts
                        (artifact_id, kind, cacheable, config, source_pins,
                         engine_versions, built_by_run, status)
                    values (:id, :kind, :cacheable, cast(:config as jsonb),
                            cast(:pins as jsonb), cast(:versions as jsonb),
                            :run_id, 'building')
                    on conflict (artifact_id) do update
                        set status = 'building',
                            built_at = null,
                            built_by_run = excluded.built_by_run
                    returning *
                    """),
                {
                    "id": derivation.id,
                    "kind": kind,
                    "cacheable": derivation.cacheable,
                    "config": canonical_json(config),
                    "pins": canonical_json(dict(source_pins or {})),
                    "versions": canonical_json(dict(engine_versions or {})),
                    "run_id": run_id,
                },
            )
            .mappings()
            .one()
        )
        for parent_id in parents:
            conn.execute(
                text("""
                    insert into triage.artifact_inputs (artifact_id, parent_id)
                    values (:id, :parent) on conflict do nothing
                    """),
                {"id": derivation.id, "parent": parent_id},
            )
    return dict(row)


def mark_built(engine: Engine, artifact_id: str, output_ref: str | None = None) -> None:
    with engine.begin() as conn:
        updated = conn.execute(
            text("""
                update triage.artifacts
                set status = 'built', built_at = now(),
                    output_ref = coalesce(:output_ref, output_ref)
                where artifact_id = :id
                """),
            {"id": artifact_id, "output_ref": output_ref},
        ).rowcount
    if updated != 1:
        raise ValueError(
            f"Cannot mark artifact {artifact_id!r} as built: no such artifact"
            + " — was begin_artifact() called?"
        )


def mark_failed(engine: Engine, artifact_id: str) -> None:
    with engine.begin() as conn:
        updated = conn.execute(
            text(
                "update triage.artifacts set status = 'failed' where artifact_id = :id"
            ),
            {"id": artifact_id},
        ).rowcount
    if updated != 1:
        raise ValueError(
            f"Cannot mark artifact {artifact_id!r} as failed: no such artifact"
        )


_CLOSURE_SQL = """
with recursive walk as (
    select a.artifact_id, a.kind, a.status, a.cacheable, 0 as depth
    from triage.artifacts a
    where a.artifact_id = :id
    union all
    select n.artifact_id, n.kind, n.status, n.cacheable, walk.depth + 1
    from walk
    join triage.artifact_inputs e on e.{near} = walk.artifact_id
    join triage.artifacts n on n.artifact_id = e.{far}
)
select artifact_id, kind, status, cacheable, min(depth) as depth
from walk
group by artifact_id, kind, status, cacheable
order by depth, artifact_id
"""


def closure(engine: Engine, artifact_id: str) -> list[dict[str, Any]]:
    """The artifact plus its full upstream input closure (provenance)."""
    sql = _CLOSURE_SQL.format(near="artifact_id", far="parent_id")
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"id": artifact_id}).mappings().all()
    return [dict(row) for row in rows]


def dependents(engine: Engine, artifact_id: str) -> list[dict[str, Any]]:
    """The artifact plus its full downstream cone (what a change invalidates)."""
    sql = _CLOSURE_SQL.format(near="parent_id", far="artifact_id")
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"id": artifact_id}).mappings().all()
    return [dict(row) for row in rows]
