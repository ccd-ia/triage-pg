"""Protected-groups ingestion — the ``bias_config`` block (ADR-0007, v1-release plan P2).

Closes the "no ingestion path" gap: before this module, ``triage.protected_groups`` was
only ever populated by hand (or by tests), so the SQL bias metrics could not run
end-to-end from a config. The block mirrors the cohort/label contract — a templated
query the user owns, run per ``as_of_date``::

    bias_config:
      query: |             # {as_of_date} required; returns entity_id + one column
        select entity_id, race, sex             #   per protected attribute
        from ontology.demographics
        where knowledge_date < '{as_of_date}'
      parameter: 100_abs   # the top-k cut the audit runs at (required)
      ref_groups: {race: White}   # optional reference pins; default = largest group
      tau: 0.8                    # optional fairness threshold (four-fifths rule)
      intervention: punitive      # optional: punitive | assistive | representation

The wide result is melted to the long ``protected_groups`` shape
(``entity_id, as_of_date, attribute_name, attribute_value``) and upserted — re-running
an experiment refreshes attribute values idempotently. ``bias_config`` is
identity-neutral (NOT part of the ADR-0022 problem hash): it observes the problem, it
does not define it.

``intervention`` routes attention, not math: it maps to the disparity metric the
fairness tree says matters for that intervention type (docs/fairness.md) and preselects
the dashboard wizard — it never hides the other metrics.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from triage.logging import get_logger

logger = get_logger(__name__)

# The Aequitas fairness tree, operationalized (docs/fairness.md): which disparity family
# matters for which intervention type. Used by the wizard preselect + docs; not by SQL.
INTERVENTION_PRIMARY_METRIC = {
    "punitive": "fpr",  # a wrong flag causes the harm
    "assistive": "fnr",  # a missed case causes the harm
    "representation": "selection_rate",  # who gets picked at all
}


def validate_bias_config(bias_config: Mapping[str, Any]) -> None:
    """Fail fast (before any build) on a malformed ``bias_config``.

    Raises ``ValueError`` with the offending key — the structured-errors twin lives in
    :func:`triage.adapters.run.validate_experiment_config` (the webapp dry-run).
    """
    query = bias_config.get("query")
    if not query:
        raise ValueError(
            "bias_config needs a 'query' returning entity_id + one column per"
            " protected attribute"
        )
    if "{as_of_date}" not in query:
        raise ValueError(
            "the bias_config query must contain the {as_of_date} placeholder"
        )
    if not bias_config.get("parameter"):
        raise ValueError(
            "bias_config needs 'parameter' — the top-k cut the audit runs at"
            " (e.g. '100_abs' or '10_pct')"
        )
    tau = bias_config.get("tau", 0.8)
    if not isinstance(tau, (int, float)) or isinstance(tau, bool) or not 0 < tau <= 1:
        raise ValueError(
            f"bias_config.tau must be a number in (0, 1], got {tau!r}"
            " (0.8 is the four-fifths rule)"
        )
    intervention = bias_config.get("intervention")
    if intervention is not None and intervention not in INTERVENTION_PRIMARY_METRIC:
        raise ValueError(
            f"unknown bias_config.intervention {intervention!r} — expected one of"
            f" {sorted(INTERVENTION_PRIMARY_METRIC)}"
        )
    ref_groups = bias_config.get("ref_groups")
    if ref_groups is not None and not isinstance(ref_groups, Mapping):
        raise ValueError(
            "bias_config.ref_groups must be a mapping {attribute: reference_value},"
            " e.g. {race: White}"
        )


def ingest_protected_groups(
    db_engine,
    query_template: str,
    as_of_dates: Sequence[Any],
) -> int:
    """Run the ``bias_config`` query per as_of_date and upsert ``protected_groups``.

    The query must return an ``entity_id`` column; every OTHER column is a protected
    attribute, melted wide → long (values cast to text — 'unknown'/NULL values are
    skipped rather than stored as the string 'None'). Upsert on the
    ``(entity_id, as_of_date, attribute_name)`` PK makes re-runs idempotent.

    Returns the number of (entity, date, attribute) rows written.
    """
    if "{as_of_date}" not in query_template:
        raise ValueError(
            "the bias_config query must contain the {as_of_date} placeholder"
        )

    written = 0
    with db_engine.connection() as conn:
        for as_of_date in as_of_dates:
            date_str = str(as_of_date)
            rows = conn.execute(query_template.format(as_of_date=date_str)).fetchall()
            if not rows:
                logger.warning(
                    f"bias_config query returned no rows for as_of_date={date_str}"
                )
                continue
            columns = list(rows[0].keys())
            if "entity_id" not in columns:
                raise ValueError(
                    "the bias_config query must return an 'entity_id' column"
                    + f" (got: {sorted(columns)})"
                )
            attributes = [c for c in columns if c != "entity_id"]
            if not attributes:
                raise ValueError(
                    "the bias_config query returned only entity_id — it must return at"
                    " least one protected-attribute column (e.g. race, sex)"
                )
            params = [
                {
                    "e": row["entity_id"],
                    "d": date_str,
                    "a": attr,
                    "v": str(row[attr]),
                }
                for row in rows
                for attr in attributes
                if row[attr] is not None
            ]
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into triage.protected_groups"
                    " (entity_id, as_of_date, attribute_name, attribute_value)"
                    " values (%(e)s, %(d)s, %(a)s, %(v)s)"
                    " on conflict (entity_id, as_of_date, attribute_name)"
                    " do update set attribute_value = excluded.attribute_value",
                    params,
                )
            written += len(params)
    logger.info(
        f"protected_groups ingestion: {written} (entity, date, attribute) row(s)"
        + f" over {len(as_of_dates)} as_of_date(s)"
    )
    return written
