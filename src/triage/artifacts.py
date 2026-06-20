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
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from psycopg_pool import ConnectionPool

from triage.derivation import Derivation, canonical_json
from triage.logging import get_logger

logger = get_logger(__name__)


def get_artifact(pool: ConnectionPool, artifact_id: str) -> dict[str, Any] | None:
    with pool.connection() as conn:
        row = conn.execute(
            "select * from triage.artifacts where artifact_id = %(id)s",
            {"id": artifact_id},
        ).fetchone()
    return dict(row) if row else None


def cache_hit(
    pool: ConnectionPool, derivation: Derivation, policy: str = "exact"
) -> dict[str, Any] | None:
    """Return the built artifact row for a derivation, or None to build.

    Volatile derivations never hit (their inputs are unpinned — the recorded
    output may be stale); neither do rows still building or failed.

    policy (ADR-0016):
        "exact"   — strict identity only (default).
        "logical" — if the strict id misses, fall back to the latest built
                    artifact with the same logical_id (identical config, pins,
                    and logical ancestry; only engine versions differ), with a
                    loud warning. Operator escape hatch for known-benign engine
                    bumps; never the silent default.
    """
    if policy not in ("exact", "logical"):
        raise ValueError(
            f"Unknown cache policy {policy!r}; expected 'exact' or 'logical'"
        )
    if not derivation.cacheable:
        logger.info(
            f"Derivation {derivation.id[:12]}… is volatile (unpinned inputs)"
            + " — skipping cache lookup, rebuilding"
        )
        return None
    artifact = get_artifact(pool, derivation.id)
    if artifact is not None and artifact["status"] == "built":
        return artifact
    if policy == "logical":
        with pool.connection() as conn:
            row = conn.execute(
                """
                    select * from triage.artifacts
                    where logical_id = %(logical_id)s
                      and artifact_id <> %(id)s
                      and status = 'built'
                      and cacheable
                    order by built_at desc
                    limit 1
                    """,
                {"logical_id": derivation.logical_id, "id": derivation.id},
            ).fetchone()
        if row is not None:
            logger.warning(
                f"ENGINE-DRIFT REUSE: derivation {derivation.id[:12]}… missed,"
                + f" reusing artifact {row['artifact_id'][:12]}… built with"
                + f" engine versions {row['engine_versions']} (policy='logical')."
                + " Outputs may differ under the current engines — rebuild with"
                + " policy='exact' to be certain."
            )
            return dict(row)
    return None


def begin_artifact(
    pool: ConnectionPool,
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
    with pool.connection() as conn:
        row = conn.execute(
            """
                insert into triage.artifacts
                    (artifact_id, logical_id, kind, cacheable, config,
                     source_pins, engine_versions, built_by_run, status)
                values (%(id)s, %(logical_id)s, %(kind)s, %(cacheable)s,
                        cast(%(config)s as jsonb), cast(%(pins)s as jsonb),
                        cast(%(versions)s as jsonb), %(run_id)s, 'building')
                on conflict (artifact_id) do update
                    set status = 'building',
                        built_at = null,
                        built_by_run = excluded.built_by_run
                returning *
                """,
            {
                "id": derivation.id,
                "logical_id": derivation.logical_id,
                "kind": kind,
                "cacheable": derivation.cacheable,
                "config": canonical_json(config),
                "pins": canonical_json(dict(source_pins or {})),
                "versions": canonical_json(dict(engine_versions or {})),
                "run_id": run_id,
            },
        ).fetchone()
        for parent_id in parents:
            conn.execute(
                """
                    insert into triage.artifact_inputs (artifact_id, parent_id)
                    values (%(id)s, %(parent)s) on conflict do nothing
                    """,
                {"id": derivation.id, "parent": parent_id},
            )
    return dict(row)


def mark_built(
    pool: ConnectionPool, artifact_id: str, output_ref: str | None = None
) -> None:
    with pool.connection() as conn:
        updated = conn.execute(
            """
                update triage.artifacts
                set status = 'built', built_at = now(),
                    output_ref = coalesce(%(output_ref)s, output_ref)
                where artifact_id = %(id)s
                """,
            {"id": artifact_id, "output_ref": output_ref},
        ).rowcount
    if updated != 1:
        raise ValueError(
            f"Cannot mark artifact {artifact_id!r} as built: no such artifact"
            + " — was begin_artifact() called?"
        )


def mark_failed(pool: ConnectionPool, artifact_id: str) -> None:
    with pool.connection() as conn:
        updated = conn.execute(
            "update triage.artifacts set status = 'failed' where artifact_id = %(id)s",
            {"id": artifact_id},
        ).rowcount
    if updated != 1:
        raise ValueError(
            f"Cannot mark artifact {artifact_id!r} as failed: no such artifact"
        )


def record_use(pool: ConnectionPool, run_id: str, artifact_ids: Sequence[str]) -> None:
    """Record that a run used these artifacts — built OR cache-hit (ADR-0017).

    These usage edges, not ``built_by_run``, are the GC root evidence: a run
    depends on every artifact it consumed, including ones built by an earlier,
    possibly later-archived run.
    """
    with pool.connection() as conn:
        for artifact_id in artifact_ids:
            conn.execute(
                """
                    insert into triage.run_artifacts (run_id, artifact_id)
                    values (%(run_id)s, %(artifact_id)s) on conflict do nothing
                    """,
                {"run_id": run_id, "artifact_id": artifact_id},
            )


def archive_experiment(pool: ConnectionPool, experiment_hash: str) -> None:
    """Soft-archive an experiment — removes it from the GC root set.

    Idempotent: re-archiving keeps the original timestamp. Reversible until a
    sweep actually collects (set archived_at back to null to restore).
    """
    with pool.connection() as conn:
        updated = conn.execute(
            """
                update triage.experiments
                set archived_at = coalesce(archived_at, now())
                where experiment_hash = %(hash)s
                """,
            {"hash": experiment_hash},
        ).rowcount
    if updated != 1:
        raise ValueError(
            f"Cannot archive experiment {experiment_hash!r}: no such experiment"
        )


# Liveness (ADR-0017): roots are (a) artifacts used by runs of non-archived
# experiments (runs without an experiment are conservatively live) and
# (b) predicted models (append-only predictions pin them regardless of
# experiment lifecycle). Live = roots plus their full upstream closure.
_DEAD_SQL = """
with recursive roots as (
    select distinct ra.artifact_id
    from triage.run_artifacts ra
    join triage.runs r using (run_id)
    left join triage.experiments e using (experiment_hash)
    where r.experiment_hash is null or e.archived_at is null
    union
    select m.model_hash
    from triage.models m
    where exists (select 1 from triage.predictions p where p.model_id = m.model_id)
),
live as (
    select artifact_id from roots
    union
    select i.parent_id
    from live l
    join triage.artifact_inputs i on i.artifact_id = l.artifact_id
)
select a.*
from triage.artifacts a
where a.status = any(%(statuses)s)
  and coalesce(a.built_at, a.created_at)
        <= now() - make_interval(days => %(min_age_days)s)
  and not exists (select 1 from live where live.artifact_id = a.artifact_id)
order by a.kind, a.artifact_id
"""


def _dead_artifacts(
    pool: ConnectionPool, statuses: Sequence[str], min_age_days: int
) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            _DEAD_SQL,
            {"statuses": list(statuses), "min_age_days": min_age_days},
        ).fetchall()
    return [dict(row) for row in rows]


def gc_candidates(pool: ConnectionPool, min_age_days: int = 0) -> list[dict[str, Any]]:
    """Built artifacts that are dead: unreachable from any GC root."""
    return _dead_artifacts(pool, statuses=["built"], min_age_days=min_age_days)


def collect(pool: ConnectionPool, artifact_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Output GC: delete in-PG outputs and mark rows 'collected' (ADR-0017).

    Rows, lineage, and pins stay — provenance is never collected, and a
    collected artifact transparently rebuilds on its next cache miss.

    cohort/labels date-slices are deleted here. File-backed outputs (matrices,
    models) and adapter-owned feature tables are returned as
    ``{artifact_id, kind, output_ref}`` for the caller to delete through the
    storage layer — their rows are still marked 'collected' first, which is
    safe: a leftover file is overwritten on rebuild, never served stale.
    """
    needs_external_deletion: list[dict[str, Any]] = []
    with pool.connection() as conn:
        for artifact_id in artifact_ids:
            row = conn.execute(
                "select kind, status, output_ref from triage.artifacts"
                + " where artifact_id = %(id)s",
                {"id": artifact_id},
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Cannot collect artifact {artifact_id!r}: no such artifact"
                )
            if row["status"] != "built":
                raise ValueError(
                    f"Cannot collect artifact {artifact_id!r} with status"
                    + f" {row['status']!r}: only 'built' outputs are collectible"
                )
            if row["kind"] == "cohort":
                conn.execute(
                    "delete from triage.cohorts where cohort_hash = %(id)s",
                    {"id": artifact_id},
                )
            elif row["kind"] == "labels":
                conn.execute(
                    "delete from triage.labels where label_hash = %(id)s",
                    {"id": artifact_id},
                )
            else:
                needs_external_deletion.append(
                    {
                        "artifact_id": artifact_id,
                        "kind": row["kind"],
                        "output_ref": row["output_ref"],
                    }
                )
            conn.execute(
                "update triage.artifacts set status = 'collected'"
                + " where artifact_id = %(id)s",
                {"id": artifact_id},
            )
    logger.info(
        f"Collected {len(artifact_ids)} artifact(s);"
        + f" {len(needs_external_deletion)} file-backed output(s) need storage-layer deletion"
    )
    return needs_external_deletion


def _delete_output_file(output_ref: str) -> bool:
    """Delete one file-backed artifact output, returning whether a file was removed.

    Matches the greenfield write path: matrices/models are plain filesystem paths
    (``adapters/matrix.py`` Parquet, ``adapters/model.py`` joblib), with ``s3://`` URIs
    supported for future remote storage. A bare path or ``file://`` URI is a local file;
    anything else dispatches by scheme. Returns ``False`` when the file is already absent
    (collect already marked the row 'collected'; a leftover would only be overwritten on
    rebuild). Real I/O errors propagate (fail fast, CLAUDE.md error policy).
    """
    parsed = urlparse(output_ref)
    if parsed.scheme == "s3":
        import s3fs

        fs = s3fs.S3FileSystem()
        if not fs.exists(output_ref):
            return False
        fs.rm(output_ref)
        return True
    # local filesystem: a bare path (scheme '') or a file:// URI
    path = Path(parsed.path) if parsed.scheme == "file" else Path(output_ref)
    if not path.exists():
        return False
    path.unlink()
    return True


def delete_outputs(external: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Delete the file-backed outputs returned by :func:`collect` (ADR-0017).

    ``external`` is collect's ``[{artifact_id, kind, output_ref}, ...]``. This is the
    storage-layer deletion step that derivation-dag.md §7 deferred "until the storage
    adapter lands". A missing file is logged and skipped, never an error; a row without an
    ``output_ref`` is logged and skipped. Returns ``{'deleted': [...], 'absent': [...]}``.
    """
    deleted: list[str] = []
    absent: list[str] = []
    for item in external:
        ref = item.get("output_ref")
        if not ref:
            logger.warning(
                f"Artifact {item['artifact_id']} ({item['kind']}) is file-backed but has"
                + " no output_ref; nothing to delete"
            )
            continue
        if _delete_output_file(ref):
            deleted.append(ref)
        else:
            absent.append(ref)
            logger.warning(
                f"Output already absent for {item['kind']} {item['artifact_id']}: {ref}"
            )
    logger.info(
        f"Deleted {len(deleted)} file-backed output(s);"
        + f" {len(absent)} already absent"
    )
    return {"deleted": deleted, "absent": absent}


def purge(pool: ConnectionPool, min_age_days: int = 0) -> list[str]:
    """Deep GC: delete the rows of dead collected/failed artifacts (ADR-0017).

    Recomputes deadness itself (defense in depth) — only artifacts that are
    both unreachable and already collected (or failed) are removed. Deletion
    runs bottom-up (leaves of the dead subgraph first): the RESTRICT on
    artifact_inputs.parent_id forbids removing a parent whose children still
    reference it, so a dead parent with a not-yet-purgeable child is retained
    until the child goes. The RESTRICT on predictions.model_id likewise makes
    any attempt to purge a predicted model fail loudly instead of eating
    append-only history.
    """
    dead = _dead_artifacts(
        pool, statuses=["collected", "failed"], min_age_days=min_age_days
    )
    remaining = {row["artifact_id"] for row in dead}
    purged: list[str] = []
    while remaining:
        with pool.connection() as conn:
            deleted = [
                r["artifact_id"]
                for r in conn.execute(
                    """
                        delete from triage.artifacts a
                        where a.artifact_id = any(%(ids)s)
                          and not exists (
                              select 1 from triage.artifact_inputs i
                              where i.parent_id = a.artifact_id
                          )
                        returning a.artifact_id
                        """,
                    {"ids": list(remaining)},
                ).fetchall()
            ]
        if not deleted:
            break  # the rest still have surviving children — retained for now
        purged.extend(deleted)
        remaining -= set(deleted)
    if purged:
        logger.info(f"Purged {len(purged)} dead artifact row(s)")
    if remaining:
        logger.info(
            f"Retained {len(remaining)} dead artifact row(s) still referenced"
            + " as parents by surviving children — purge again once those are"
            + " collectible"
        )
    return purged


_CLOSURE_SQL = """
with recursive walk as (
    select a.artifact_id, a.kind, a.status, a.cacheable, 0 as depth
    from triage.artifacts a
    where a.artifact_id = %(id)s
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


def closure(pool: ConnectionPool, artifact_id: str) -> list[dict[str, Any]]:
    """The artifact plus its full upstream input closure (provenance)."""
    sql = _CLOSURE_SQL.format(near="artifact_id", far="parent_id")
    with pool.connection() as conn:
        rows = conn.execute(sql, {"id": artifact_id}).fetchall()
    return [dict(row) for row in rows]


def dependents(pool: ConnectionPool, artifact_id: str) -> list[dict[str, Any]]:
    """The artifact plus its full downstream cone (what a change invalidates)."""
    sql = _CLOSURE_SQL.format(near="parent_id", far="artifact_id")
    with pool.connection() as conn:
        rows = conn.execute(sql, {"id": artifact_id}).fetchall()
    return [dict(row) for row in rows]
