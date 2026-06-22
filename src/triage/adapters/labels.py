"""Greenfield label builder — runs the derivation-DAG lifecycle (ADR-0013/0015, ADR-0010).

A label is a forward-looking target per ``(entity_id, as_of_date, label_timespan)``. Labels
*depend on* the cohort (they are computed over the same roster), so the labels artifact
carries the cohort artifact as its single parent — that ``triage.artifact_inputs`` edge is
the "labels-depend-on-cohort" provenance, and it makes the label derivation hash chain over
the cohort's identity (a cohort change rebuilds labels).

The label-query columns follow ``problem_type`` (ADR-0010, CLAUDE.md gotcha):

* ``classification`` / ``regression_ranking`` / ``regression`` -> ``entity_id, outcome``
* ``survival`` -> ``entity_id, duration, event_observed``

The query is templated SQL with two placeholders, ``{as_of_date}`` and ``{label_timespan}``;
it is wrapped as a subquery and projected into ``triage.labels``. ``{as_of_date}`` is
substituted as a bare quoted literal (``'2014-01-01'``) and ``{label_timespan}`` as
``interval '6 months'``; a query that does date arithmetic must cast the date explicitly
(``date {as_of_date} + {label_timespan}``) — an untyped literal next to ``+ interval`` is
otherwise read as an interval. Lifecycle mirrors
:mod:`triage.adapters.cohort`: derive -> cache_hit -> begin_artifact -> populate -> mark_built
-> record_use, with mark_failed + re-raise on any error (fail fast).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from psycopg_pool import ConnectionPool

from triage.artifacts import (
    begin_artifact,
    cache_hit,
    get_artifact,
    mark_built,
    mark_failed,
    record_use,
)
from triage.derivation import Derivation, derive, engine_versions_for
from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = ["build_labels", "LABELS_KIND", "LABELS_OUTPUT_REF"]

LABELS_KIND = "labels"
LABELS_OUTPUT_REF = "triage.labels"

# problem_type -> the label-query output columns it must supply (besides entity_id),
# and the matching triage.labels target columns.
_OUTCOME_TYPES = frozenset({"classification", "regression_ranking", "regression"})
_SURVIVAL_TYPE = "survival"

_REQUIRED_PLACEHOLDERS = ("{as_of_date}", "{label_timespan}")


def _validate_template(label_query_template: str) -> None:
    for placeholder in _REQUIRED_PLACEHOLDERS:
        if placeholder not in label_query_template:
            raise ValueError(
                f"label_query_template must contain the {placeholder} placeholder"
                + " (CLAUDE.md label-query contract); got: "
                + repr(label_query_template)
            )
    if ";" in label_query_template:
        raise ValueError(
            "label_query_template must be a single SELECT with no ';' — it is wrapped"
            + " as a subquery; got: "
            + repr(label_query_template)
        )


def _label_projection(problem_type: str) -> tuple[str, str]:
    """Return (target_columns, select_columns) for ``problem_type`` (ADR-0010).

    Both share the ``cohort_hash``/``as_of_date``/``label_timespan`` keys; they differ
    only in which value columns the wrapped subquery is expected to supply.
    """
    if problem_type in _OUTCOME_TYPES:
        return (
            "label_hash, entity_id, as_of_date, label_timespan, outcome",
            "%(label_hash)s, sub.entity_id, %(as_of_date)s,"
            + " cast(%(label_timespan)s as interval), sub.outcome",
        )
    if problem_type == _SURVIVAL_TYPE:
        return (
            "label_hash, entity_id, as_of_date, label_timespan, duration, event_observed",
            "%(label_hash)s, sub.entity_id, %(as_of_date)s,"
            + " cast(%(label_timespan)s as interval), sub.duration, sub.event_observed",
        )
    raise ValueError(
        f"unknown problem_type {problem_type!r}; expected one of"
        + f" {sorted(_OUTCOME_TYPES) + [_SURVIVAL_TYPE]} (ADR-0010)"
    )


def build_labels(
    db_engine: ConnectionPool,
    run_id: str,
    cohort_artifact_id: str,
    label_query_template: str,
    as_of_dates: Sequence[date],
    label_timespans: Sequence[str],
    problem_type: str,
    config: Mapping[str, Any],
    source_pins: Mapping[str, str | None] | None = None,
    policy: str = "exact",
) -> str:
    """Build (or reuse) the labels artifact and return its ``artifact_id``.

    The labels artifact's single parent is the cohort artifact (``cohort_artifact_id``):
    that edge encodes labels-depend-on-cohort and chains the labels identity over the
    cohort's. Populates ``triage.labels`` for every ``(as_of_date, label_timespan)`` pair,
    routing columns by ``problem_type`` (ADR-0010).

    Args:
        db_engine: project-database engine (greenfield ``triage.*`` schema).
        run_id: the owning run; must already exist (FK).
        cohort_artifact_id: the parent cohort artifact (must already be built).
        label_query_template: SELECT returning ``entity_id`` plus the outcome columns
            dictated by ``problem_type``; carries ``{as_of_date}`` and
            ``{label_timespan}`` placeholders.
        as_of_dates: the split's ``as_of_times``.
        label_timespans: the label horizons (e.g. ``['6 months']``).
        problem_type: ADR-0010 discriminator selecting the outcome vs survival columns.
        config: the labels' own canonical config slice (enters identity).
        source_pins: declared-source -> version pins (volatile if unpinned).
        policy: cache lookup policy ('exact' default).

    Returns:
        The labels ``artifact_id`` (== ``triage.labels.label_hash``).
    """
    _validate_template(label_query_template)
    if not as_of_dates:
        raise ValueError("build_labels requires at least one as_of_date")
    if not label_timespans:
        raise ValueError("build_labels requires at least one label_timespan")
    target_columns, select_columns = _label_projection(problem_type)

    parent = get_artifact(db_engine, cohort_artifact_id)
    if parent is None:
        raise ValueError(
            f"cohort artifact {cohort_artifact_id!r} does not exist — build the cohort"
            + " before its labels (the labels->cohort edge requires the parent row)"
        )
    # The parent's identity is what chains into the labels hash. Reconstruct just enough
    # of its Derivation (id + cacheability) so derive() can fold it in; logical_id is
    # carried through for the fallback policy chain (ADR-0016).
    cohort_derivation = Derivation(
        id=parent["artifact_id"],
        logical_id=parent["logical_id"],
        cacheable=parent["cacheable"],
    )

    canonical_config = dict(config)
    derivation = derive(
        kind=LABELS_KIND,
        config=canonical_config,
        parents=[cohort_derivation],
        source_pins=source_pins,
        engine_versions=engine_versions_for(LABELS_KIND),
    )

    hit = cache_hit(db_engine, derivation, policy=policy)
    if hit is not None:
        logger.info(f"Labels {derivation.id[:12]}… already built — reusing (cache hit)")
        record_use(db_engine, run_id, [hit["artifact_id"]])
        return hit["artifact_id"]

    begin_artifact(
        db_engine,
        derivation,
        kind=LABELS_KIND,
        config=canonical_config,
        source_pins=source_pins,
        engine_versions=engine_versions_for(LABELS_KIND),
        run_id=run_id,
        parents=[cohort_artifact_id],
    )
    artifact_id = derivation.id
    try:
        with db_engine.connection() as conn:
            for as_of_date in as_of_dates:
                for label_timespan in label_timespans:
                    rendered = label_query_template.format(
                        as_of_date=f"'{as_of_date}'",
                        label_timespan=f"interval '{label_timespan}'",
                    )
                    # The rendered user SQL is embedded directly; psycopg3 reads ``%`` as
                    # the parameter marker, so literal ``%`` in the user's query (LIKE
                    # patterns etc.) is doubled to ``%%``. The %(name)s binds in
                    # select_columns stay as-is.
                    conn.execute(
                        f"insert into triage.labels ({target_columns})"
                        + f" select {select_columns} from ({rendered.replace('%', '%%')}) sub"
                        + " on conflict do nothing",
                        {
                            "label_hash": artifact_id,
                            "as_of_date": as_of_date,
                            "label_timespan": label_timespan,
                        },
                    )
        mark_built(
            db_engine,
            artifact_id,
            output_ref=LABELS_OUTPUT_REF,
            kind=LABELS_KIND,
            run_id=run_id,
        )
    except Exception:
        mark_failed(db_engine, artifact_id, kind=LABELS_KIND, run_id=run_id)
        raise

    record_use(db_engine, run_id, [artifact_id])
    logger.info(
        f"Built labels {artifact_id[:12]}… ({problem_type}) over"
        + f" {len(as_of_dates)} as_of_date(s) × {len(label_timespans)} timespan(s)"
    )
    return artifact_id
