"""Crosstabs — what distinguishes the selected top-k from the rest? (plan P5)

For each feature and prediction date: mean / std / nonzero-rate among the SELECTED
(top-k at the cut) vs the REST of the scored population, plus their ratio. Descriptive
by design — the |log ratio| ranking answers "which features characterize the list";
significance testing is deliberately out (see the plan's Questionables).
"""

from __future__ import annotations

from typing import Any

from triage.diagnostics.matrixio import (
    load_matrix_context,
    scored_dates,
    top_k_entities,
)
from triage.logging import get_logger

logger = get_logger(__name__)

_STATS = ("mean", "std", "nonzero_rate")


def _column_stats(values) -> dict[str, float | None]:
    """mean / std / nonzero-rate of a numeric polars Series (NULLs dropped)."""
    import numpy as np

    arr = values.drop_nulls().to_numpy().astype(float)
    if arr.size == 0:
        return {"mean": None, "std": None, "nonzero_rate": None}
    return {
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0,
        "nonzero_rate": float(np.mean(arr != 0)),
    }


def compute_crosstabs(
    db_engine,
    model_id: int,
    parameter: str = "100_abs",
    split_kind: str = "test",
    as_of_date: Any = None,
) -> int:
    """Compute + persist crosstab rows for every prediction date (or one). Returns rows written."""
    ctx = load_matrix_context(db_engine, model_id, split_kind)
    dates = (
        [as_of_date]
        if as_of_date is not None
        else scored_dates(db_engine, model_id, split_kind)
    )
    written = 0
    for date in dates:
        selected, k = top_k_entities(db_engine, model_id, split_kind, date, parameter)
        if not selected:
            logger.warning(f"model {model_id}: no top-k at {date} — skipping crosstabs")
            continue
        day = ctx.frame.filter(ctx.frame["as_of_date"].cast(str) == str(date))
        if day.height == 0:
            logger.warning(
                f"model {model_id}: matrix has no rows at {date} — skipping crosstabs"
            )
            continue
        sel_mask = day["entity_id"].is_in(list(selected))
        sel_frame, rest_frame = day.filter(sel_mask), day.filter(~sel_mask)
        params: list[dict[str, Any]] = []
        for feature in ctx.feature_names:
            sel = _column_stats(sel_frame[feature])
            rest = _column_stats(rest_frame[feature])
            for stat in _STATS:
                s, r = sel[stat], rest[stat]
                params.append(
                    {
                        "m": model_id,
                        "sk": split_kind,
                        "d": str(date),
                        "p": parameter,
                        "f": feature,
                        "stat": stat,
                        "sv": s,
                        "rv": r,
                        "ratio": (
                            (s / r) if (s is not None and r not in (None, 0)) else None
                        ),
                    }
                )
        with db_engine.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "insert into triage.crosstabs"
                " (model_id, split_kind, as_of_date, parameter, feature, stat,"
                "  selected_value, rest_value, ratio)"
                " values (%(m)s, cast(%(sk)s as triage.split_kind), %(d)s, %(p)s,"
                "         %(f)s, %(stat)s, %(sv)s, %(rv)s, %(ratio)s)"
                " on conflict (model_id, split_kind, as_of_date, parameter, feature, stat)"
                " do update set selected_value = excluded.selected_value,"
                "               rest_value = excluded.rest_value,"
                "               ratio = excluded.ratio,"
                "               computed_at = now()",
                params,
            )
        written += len(params)
        logger.info(
            f"crosstabs: model {model_id} @ {date} (k={k}) — {len(params)} row(s)"
        )
    return written
