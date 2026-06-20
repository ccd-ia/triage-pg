"""Append-only predictions + SQL window-function ranking (ADR-0006, ADR-0010).

This is the *greenfield* prediction-write/read surface, targeting the
``triage.*`` schema created by the ``0001_initial_triage_schema`` migration —
NOT the inherited ORM schema (``test_results`` / ``train_results``) that
``Predictor`` in :mod:`triage.component.catwalk.predictors` still writes to.

Two concerns, cleanly separated:

* **Write (ADR-0006).** ``record_predictions`` INSERTs one row per score into
  ``triage.predictions`` and never updates or deletes. The table carries a
  ``scored_at timestamptz default now()`` and is partitioned by range on it;
  re-scoring the same ``(model_id, entity_id, as_of_date)`` *appends* a new row
  with a later ``scored_at``. There are no rank columns on the table — ranking
  is a read concern.

* **Read (ADR-0010).** ``fetch_ranks`` selects from ``triage.prediction_ranks``,
  the view that picks the latest score per ``(model_id, entity_id, as_of_date)``
  (via ``triage.latest_predictions``) and ranks it with window functions:

  .. code-block:: sql

      row_number()   over w as rank_abs,
      percent_rank() over w as rank_pct
      window w as (partition by model_id, as_of_date
                   order by score desc, entity_id)

  Ties are broken *deterministically* by ``entity_id`` (schema-design §8.3);
  there is no random sort seed and no ``num_sort_trials`` (those were dropped
  with the stochastic-evaluation machinery).

Why a standalone module rather than folding this into ``Predictor``: the
inherited ``Predictor`` write path is coupled to the old ORM ``Model`` /
``TestPrediction`` tables, and a greenfield prediction row requires the
``artifacts -> model_groups -> models`` lineage chain (``predictions.model_id``
-> ``models.model_id`` -> ``models.model_hash`` -> ``artifacts.artifact_id``).
Populating that chain from training is Phase-F (adapter + builder) work. This
module is the validated greenfield capability that the Phase-F builder will
call; until then it stands alone with its own validation test.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from psycopg_pool import ConnectionPool

from triage.logging import get_logger

logger = get_logger(__name__)

# split_kind values permitted by the triage.split_kind enum (0001 migration).
VALID_SPLIT_KINDS = frozenset({"train", "test", "validation", "production"})


def record_predictions(
    db_engine: ConnectionPool,
    model_id: int,
    split_kind: str,
    scores: Iterable[Mapping[str, Any]],
    *,
    matrix_uuid: str | None = None,
) -> int:
    """Append score rows to ``triage.predictions`` (ADR-0006, append-only).

    Every call INSERTs new rows; nothing is updated or deleted, so re-scoring
    the same entities at the same ``as_of_date`` accumulates history that the
    ``triage.latest_predictions`` view later collapses to the newest
    ``scored_at``.

    Args:
        db_engine: SQLAlchemy engine bound to a per-project results database
            whose ``triage`` schema has been created (0001 migration).
        model_id: ``triage.models.model_id`` the scores belong to.
        split_kind: one of :data:`VALID_SPLIT_KINDS`.
        scores: iterable of mappings, each with ``entity_id`` (int),
            ``as_of_date`` (``datetime.date`` or ISO ``YYYY-MM-DD`` string) and
            ``score`` (float). ``scored_at`` is assigned by the database
            default (``now()``); do not pass it.
        matrix_uuid: optional ``triage.matrices.matrix_uuid`` the scores came
            from.

    Returns:
        The number of prediction rows inserted.

    Raises:
        ValueError: if ``split_kind`` is not a recognized enum value.
    """
    if split_kind not in VALID_SPLIT_KINDS:
        raise ValueError(
            f"split_kind {split_kind!r} is not one of {sorted(VALID_SPLIT_KINDS)} "
            "(triage.split_kind enum, 0001 migration)"
        )

    rows = [
        {
            "model_id": int(model_id),
            "entity_id": int(s["entity_id"]),
            "as_of_date": s["as_of_date"],
            "split_kind": split_kind,
            "score": float(s["score"]),
            "matrix_uuid": matrix_uuid,
        }
        for s in scores
    ]
    if not rows:
        logger.debug(
            f"record_predictions called for model {model_id} with no scores; nothing inserted"
        )
        return 0

    # No scored_at in the column list -> the table default (now()) fills it, so
    # re-scoring always lands a distinct, later row (append-only, ADR-0006).
    insert = (
        "insert into triage.predictions "
        "(model_id, entity_id, as_of_date, split_kind, score, matrix_uuid) "
        "values (%(model_id)s, %(entity_id)s, %(as_of_date)s, "
        "cast(%(split_kind)s as triage.split_kind), %(score)s, "
        "cast(%(matrix_uuid)s as uuid))"
    )
    with db_engine.connection() as conn, conn.cursor() as cur:
        cur.executemany(insert, rows)

    logger.debug(
        f"Appended {len(rows)} prediction rows for model {model_id} ({split_kind})"
    )
    return len(rows)


def fetch_ranks(
    db_engine: ConnectionPool,
    model_id: int,
    as_of_date: date | str,
) -> list[dict[str, Any]]:
    """Read deterministic ranks for one (model, as_of_date) from the view.

    Reads ``triage.prediction_ranks`` (latest score per entity, ranked by
    ``score desc, entity_id``). Returns rows ordered by ``rank_abs`` so the
    caller gets a ready-to-use prioritization list (ADR-0010).

    Args:
        db_engine: engine bound to the per-project results database.
        model_id: ``triage.models.model_id``.
        as_of_date: the scoring date to rank within.

    Returns:
        A list of dicts with keys ``entity_id``, ``as_of_date``,
        ``split_kind``, ``score``, ``scored_at``, ``rank_abs`` (1-based,
        ``row_number``) and ``rank_pct`` (``percent_rank``).
    """
    query = (
        "select entity_id, as_of_date, split_kind, score, scored_at, "
        "rank_abs, rank_pct "
        "from triage.prediction_ranks "
        "where model_id = %(model_id)s and as_of_date = %(as_of_date)s "
        "order by rank_abs"
    )
    with db_engine.connection() as conn:
        result = conn.execute(
            query, {"model_id": int(model_id), "as_of_date": as_of_date}
        )
        return result.fetchall()


def rank_predictions(
    db_engine: ConnectionPool,
    model_id: int,
    split_kind: str,
    scores: Sequence[Mapping[str, Any]],
    as_of_date: date | str,
    *,
    matrix_uuid: str | None = None,
) -> list[dict[str, Any]]:
    """Append scores then return their deterministic ranks in one call.

    Convenience wrapper composing :func:`record_predictions` and
    :func:`fetch_ranks` for the common "score, then rank" flow. Because the
    write is append-only and the read goes through ``latest_predictions``, the
    returned ranks reflect the scores just written (assuming they are the
    newest for those entities at ``as_of_date``).
    """
    record_predictions(
        db_engine,
        model_id,
        split_kind,
        scores,
        matrix_uuid=matrix_uuid,
    )
    return fetch_ranks(db_engine, model_id, as_of_date)
