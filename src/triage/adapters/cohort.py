"""Greenfield cohort builder — runs the derivation-DAG lifecycle (ADR-0013/0015).

A cohort is a *per-as_of_date roster* of entities (``docs/adapter-spec.md`` §2.3): the
selection mask later inner-joined against the dense featurizer matrix. This builder is the
greenfield replacement for the inherited ``entity_date_table_generators`` — it owns the
artifact lifecycle, not just the SQL:

    derivation = derive('cohort', config, parents=[], source_pins, engine_versions)
    if (hit := cache_hit(engine, derivation)) is not None:   # already built -> reuse
        record_use(run_id, [hit['artifact_id']]);  return hit['artifact_id']
    begin_artifact(...)                                       # row -> 'building'
    try:
        for as_of_date in as_of_dates:                        # populate triage.cohorts
            insert (cohort_hash=artifact_id, entity_id, as_of_date)
        mark_built(artifact_id, output_ref='triage.cohorts')
    except:
        mark_failed(artifact_id);  raise                      # fail fast (ADR error policy)
    record_use(run_id, [artifact_id])
    return artifact_id

The cohort query is templated SQL with a single ``{as_of_date}`` placeholder; it must return
one column, ``entity_id``, naming the entities in the roster at that date. The builder wraps
it as ``insert into triage.cohorts select <artifact_id>, sub.entity_id, <as_of_date> from
(<query>) sub`` so the only required output column is ``entity_id`` (CLAUDE.md cohort-query
contract).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import LiteralString, cast, Any

from triage.util.db import DictRowPool

from triage.artifacts import (
    begin_artifact,
    cache_hit,
    mark_built,
    mark_failed,
    record_use,
)
from triage.derivation import derive, engine_versions_for
from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = ["build_cohort", "COHORT_KIND", "COHORT_OUTPUT_REF"]

COHORT_KIND = "cohort"
COHORT_OUTPUT_REF = "triage.cohorts"

# A cohort query may not contain its own semicolons (it is wrapped as a subquery);
# the only placeholder it may carry is {as_of_date}.
_REQUIRED_PLACEHOLDER = "{as_of_date}"


def _validate_template(cohort_query_template: str) -> None:
    if _REQUIRED_PLACEHOLDER not in cohort_query_template:
        raise ValueError(
            "cohort_query_template must contain the {as_of_date} placeholder"
            + f" (CLAUDE.md cohort-query contract); got: {cohort_query_template!r}"
        )
    if ";" in cohort_query_template:
        raise ValueError(
            "cohort_query_template must be a single SELECT with no ';' — it is wrapped"
            + " as a subquery; got: "
            + repr(cohort_query_template)
        )


def build_cohort(
    db_engine: DictRowPool,
    run_id: str,
    cohort_query_template: str,
    as_of_dates: Sequence[date],
    config: Mapping[str, Any],
    source_pins: Mapping[str, str | None] | None = None,
    policy: str = "exact",
) -> str:
    """Build (or reuse) the cohort artifact and return its ``artifact_id``.

    Runs the full derivation lifecycle: derive identity over ``config`` + source pins
    (no parents — the cohort is a DAG root), look up the cache, and on a miss populate
    ``triage.cohorts`` for every ``as_of_date`` before marking the artifact built. The
    run's usage edge is recorded on both build and cache-hit (ADR-0017 GC roots).

    Args:
        db_engine: project-database engine (greenfield ``triage.*`` schema).
        run_id: the owning run (``triage.runs.run_id``); must already exist (FK).
        cohort_query_template: SELECT returning ``entity_id``, with one
            ``{as_of_date}`` placeholder.
        as_of_dates: the split's ``as_of_times`` (``TemporalConfig`` -> Timechop, §1).
        config: the cohort's own canonical config slice — enters its identity.
            Should already include the query template / cohort knobs.
        source_pins: declared-source -> version pins (``resolve_pins``); unpinned
            sources make the derivation volatile (never a cache hit, ADR-0014).
        policy: cache lookup policy passed to :func:`cache_hit` ('exact' default).

    Returns:
        The cohort ``artifact_id`` (== ``triage.cohorts.cohort_hash``).
    """
    _validate_template(cohort_query_template)
    if not as_of_dates:
        raise ValueError("build_cohort requires at least one as_of_date")
    # The DATES ARE IDENTITY: the artifact's rows are materialized exactly for these
    # as_of_dates, so a config-identical run with a different temporal grid must be a
    # cache MISS — reusing an artifact built for other dates silently serves empty/partial
    # cohort slices to every downstream matrix (found live: a 60-day survival grid
    # cache-hitting the 14-day EWS grid's cohort produced 0-entity test matrices).
    canonical_config = {
        **dict(config),
        "as_of_dates": sorted(d.isoformat() for d in as_of_dates),
    }

    derivation = derive(
        kind=COHORT_KIND,
        config=canonical_config,
        parents=[],
        source_pins=source_pins,
        engine_versions=engine_versions_for(COHORT_KIND),
    )

    hit = cache_hit(db_engine, derivation, policy=policy)
    if hit is not None:
        logger.info(f"Cohort {derivation.id[:12]}… already built — reusing (cache hit)")
        record_use(db_engine, run_id, [hit["artifact_id"]])
        return hit["artifact_id"]

    begin_artifact(
        db_engine,
        derivation,
        kind=COHORT_KIND,
        config=canonical_config,
        source_pins=source_pins,
        engine_versions=engine_versions_for(COHORT_KIND),
        run_id=run_id,
        parents=[],
    )
    artifact_id = derivation.id
    try:
        with db_engine.connection() as conn:
            for as_of_date in as_of_dates:
                rendered = cohort_query_template.format(as_of_date=f"'{as_of_date}'")
                # The rendered user SQL is embedded directly; psycopg3 reads ``%`` as the
                # parameter marker, so any literal ``%`` in the user's query (e.g. LIKE
                # patterns) must be doubled to ``%%`` — our own binds stay ``%(name)s``.
                # cast: the cohort query is operator-supplied template SQL — dynamic
                # by design, so psycopg's LiteralString guard cannot apply here.
                sql = cast(
                    LiteralString,
                    "insert into triage.cohorts (cohort_hash, entity_id, as_of_date)"
                    + " select %(cohort_hash)s, sub.entity_id, %(as_of_date)s"
                    + f" from ({rendered.replace('%', '%%')}) sub"
                    + " on conflict do nothing",
                )
                conn.execute(
                    sql, {"cohort_hash": artifact_id, "as_of_date": as_of_date}
                )
        mark_built(
            db_engine,
            artifact_id,
            output_ref=COHORT_OUTPUT_REF,
            kind=COHORT_KIND,
            run_id=run_id,
        )
    except Exception:
        mark_failed(db_engine, artifact_id, kind=COHORT_KIND, run_id=run_id)
        raise

    record_use(db_engine, run_id, [artifact_id])
    logger.info(
        f"Built cohort {artifact_id[:12]}… over {len(as_of_dates)} as_of_date(s)"
    )
    return artifact_id
