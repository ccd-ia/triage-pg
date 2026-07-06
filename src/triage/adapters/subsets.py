"""Subset materialization — the ``evaluation.subsets`` block (schema-design §8.6, plan P3).

A subset is a named cohort slice defined by a templated query the user owns::

    evaluation:
      subsets:
        - name: district7
          query: |
            select entity_id from ontology.service_requests
            where district = 7 and created_date < '{as_of_date}'

Each subset's identity is the sha256 over its canonical config (name + query) — the
``subset_hash`` stamped on every evaluation row it produces. Members are materialized
into ``triage.subset_members`` per as_of_date (idempotent upserts), and migration 0015's
metric functions re-rank WITHIN the membership, treating the subset as the population
(DSSG subset semantics: ``precision@100_abs`` on district 7 is the top-100 of district
7's own ranking).

Subsets are identity-neutral (NOT in the ADR-0022 problem hash): they observe the
problem on a slice, they do not define it. Full-cohort rows keep ``subset_hash = ''``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from triage.logging import get_logger

logger = get_logger(__name__)


def subset_hash_for(subset_config: Mapping[str, Any]) -> str:
    """sha256 over the canonical subset config (name + query) — the row discriminator."""
    canonical = json.dumps(
        {"name": subset_config["name"], "query": subset_config["query"]},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_subsets_config(subsets: Sequence[Mapping[str, Any]]) -> None:
    """Fail fast on a malformed ``evaluation.subsets`` list (the structured-errors twin
    lives in :func:`triage.adapters.run.validate_experiment_config`)."""
    seen_names: set[str] = set()
    for i, subset in enumerate(subsets):
        name = subset.get("name") if isinstance(subset, Mapping) else None
        if not name:
            raise ValueError(f"evaluation.subsets[{i}] needs a 'name'")
        if name in seen_names:
            raise ValueError(f"duplicate subset name {name!r} in evaluation.subsets")
        seen_names.add(name)
        query = subset.get("query")
        if not query:
            raise ValueError(f"subset {name!r} needs a 'query' returning entity_id")
        if "{as_of_date}" not in query:
            raise ValueError(
                f"subset {name!r}: the query must contain the {{as_of_date}} placeholder"
            )


def register_subsets(
    db_engine,
    subsets: Sequence[Mapping[str, Any]],
    as_of_dates: Sequence[Any],
) -> list[dict[str, str]]:
    """Register each subset (``triage.subsets``) and materialize its members per date.

    Returns ``[{name, subset_hash}]`` in config order. Idempotent: the subsets row is
    upserted and membership rows conflict-away on the PK; a subset whose query returns
    no rows for a date logs a warning (an empty slice is a legitimate, if suspicious,
    outcome — the evaluations will carry ``num_labeled = 0``).
    """
    validate_subsets_config(subsets)
    registered: list[dict[str, str]] = []
    with db_engine.connection() as conn:
        for subset in subsets:
            sh = subset_hash_for(subset)
            conn.execute(
                "insert into triage.subsets (subset_hash, config)"
                " values (%(h)s, %(c)s::jsonb)"
                " on conflict (subset_hash) do nothing",
                {"h": sh, "c": json.dumps(dict(subset))},
            )
            total = 0
            for as_of_date in as_of_dates:
                date_str = str(as_of_date)
                rows = conn.execute(
                    subset["query"].format(as_of_date=date_str)
                ).fetchall()
                if rows and "entity_id" not in rows[0]:
                    raise ValueError(
                        f"subset {subset['name']!r}: the query must return an"
                        f" 'entity_id' column (got: {sorted(rows[0].keys())})"
                    )
                if not rows:
                    logger.warning(
                        f"subset {subset['name']!r} matched no entities at"
                        + f" as_of_date={date_str}"
                    )
                    continue
                with conn.cursor() as cur:
                    cur.executemany(
                        "insert into triage.subset_members"
                        " (subset_hash, entity_id, as_of_date)"
                        " values (%(h)s, %(e)s, %(d)s)"
                        " on conflict do nothing",
                        [
                            {"h": sh, "e": row["entity_id"], "d": date_str}
                            for row in rows
                        ],
                    )
                total += len(rows)
            registered.append({"name": subset["name"], "subset_hash": sh})
            logger.info(
                f"subset {subset['name']!r} ({sh[:12]}…): {total} member row(s) over"
                + f" {len(as_of_dates)} as_of_date(s)"
            )
    return registered
