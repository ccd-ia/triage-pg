"""Shared matrix/context resolution for the diagnostics (plan P5).

Everything a diagnostic needs is resolvable from SQL + one Parquet read: the model's
scored predictions carry their ``matrix_uuid`` (ADR-0006 lineage), the ``matrices`` row
carries ``storage_uri`` + ``feature_names`` + ``label_timespan``, and the profile
storage seam reads local FS and ``s3://…`` identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from triage.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MatrixContext:
    matrix_uuid: str
    storage_uri: str
    feature_names: list[str]
    label_timespan: str | None
    frame: Any  # polars.DataFrame with entity_id / as_of_date / feature columns


def load_matrix_context(db_engine, model_id: int, split_kind: str) -> MatrixContext:
    """Resolve and load the ONE matrix the model's ``split_kind`` predictions came from.

    Raises loud on no lineage (predictions recorded without ``matrix_uuid`` — e.g.
    pre-lineage rows) and on ambiguity (two matrices for one (model, split) — the
    diagnostics refuse to mix feature geometries).
    """
    from triage.profiles.storage import read_parquet, storage_for_root

    with db_engine.connection() as conn:
        rows = conn.execute(
            "select distinct mx.matrix_uuid::text as matrix_uuid, mx.storage_uri,"
            "       mx.feature_names, mx.label_timespan::text as label_timespan"
            " from triage.predictions p"
            " join triage.matrices mx using (matrix_uuid)"
            " where p.model_id = %(m)s"
            "   and p.split_kind = cast(%(s)s as triage.split_kind)"
            "   and p.matrix_uuid is not null",
            {"m": model_id, "s": split_kind},
        ).fetchall()
    if not rows:
        raise ValueError(
            f"model {model_id} has no {split_kind!r} predictions with matrix lineage —"
            " diagnostics need the scored matrix (re-score with a current triage-pg)"
        )
    if len(rows) > 1:
        raise ValueError(
            f"model {model_id} has {len(rows)} distinct {split_kind!r} matrices —"
            " diagnostics refuse to mix feature geometries"
        )
    row = rows[0]
    storage = storage_for_root(row["storage_uri"])
    if not storage.exists(row["storage_uri"]):
        raise ValueError(
            f"model {model_id}'s scored matrix is gone from storage"
            f" ({row['storage_uri']}) — deleted, GC'd, or an OS tmp purge. Re-run the"
            " experiment to rebuild it, or diagnose a model from a current run"
            " (`triage models <experiment>` lists them)."
        )
    frame = read_parquet(storage, row["storage_uri"])
    feature_names = [c for c in (row["feature_names"] or []) if c in frame.columns]
    if not feature_names:
        raise ValueError(
            f"matrix {row['matrix_uuid']} carries no usable feature columns —"
            f" matrices.feature_names is empty or disjoint from the Parquet schema"
        )
    return MatrixContext(
        matrix_uuid=row["matrix_uuid"],
        storage_uri=row["storage_uri"],
        feature_names=feature_names,
        label_timespan=row["label_timespan"],
        frame=frame,
    )


def scored_dates(db_engine, model_id: int, split_kind: str) -> list[Any]:
    with db_engine.connection() as conn:
        return [
            r["as_of_date"]
            for r in conn.execute(
                "select distinct as_of_date from triage.predictions"
                " where model_id = %(m)s"
                "   and split_kind = cast(%(s)s as triage.split_kind)"
                " order by as_of_date",
                {"m": model_id, "s": split_kind},
            ).fetchall()
        ]


def top_k_entities(
    db_engine, model_id: int, split_kind: str, as_of_date: Any, parameter: str
) -> tuple[set[int], int]:
    """The deterministic top-k entity set at the cut, and k itself."""
    with db_engine.connection() as conn:
        k = conn.execute(
            "select triage.resolve_k(%(p)s, count(*)::int) as k"
            " from triage.prediction_ranks"
            " where model_id = %(m)s"
            "   and split_kind = cast(%(s)s as triage.split_kind)"
            "   and as_of_date = %(d)s",
            {"p": parameter, "m": model_id, "s": split_kind, "d": str(as_of_date)},
        ).fetchone()["k"]
        entities = {
            r["entity_id"]
            for r in conn.execute(
                "select entity_id from triage.prediction_ranks"
                " where model_id = %(m)s"
                "   and split_kind = cast(%(s)s as triage.split_kind)"
                "   and as_of_date = %(d)s and rank_abs <= %(k)s",
                {"m": model_id, "s": split_kind, "d": str(as_of_date), "k": k},
            ).fetchall()
        }
    return entities, int(k or 0)
