"""Evaluate model groups on the rows every one of them scored (migration 0031).

A baseline that cannot forecast a row abstains and leaves no prediction, so each model group
is evaluated on its own rows. :func:`evaluate_on_common_rows` registers the labeled rows that
every listed group scored as a Subset and evaluates each of their models on it, with the
Experiment's metric set. Audition then compares the groups on equal rows by passing that
``subset_hash``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional

from triage.component.catwalk.in_pg_evaluation import evaluate_in_db
from triage.util.db import DictRowPool, returned_row


@dataclass(frozen=True)
class CommonRows:
    """The Subset of common rows, and what was evaluated on it."""

    subset_hash: str
    name: str
    model_group_ids: list[int]
    n_rows: int
    n_dates: int
    n_evaluations: int


def experiment_groups(pool: DictRowPool, experiment_hash: str) -> list[int]:
    """Every model group with a model in the Experiment, ascending."""
    with pool.connection() as conn:
        return [
            r["model_group_id"]
            for r in conn.execute(
                "select distinct m.model_group_id from triage.models m"
                " join triage.runs r on r.run_id = m.run_id"
                " where r.experiment_hash = %(h)s order by 1",
                {"h": experiment_hash},
            ).fetchall()
        ]


def compared_groups(
    pool: DictRowPool,
    experiment_hash: str,
    model_group_ids: Optional[Sequence[int]] = None,
) -> list[int]:
    """The groups to compare: those given, else every group of the Experiment, ascending.

    Raises:
        ValueError: a group has no model in the Experiment, or fewer than two remain.
    """
    in_experiment = experiment_groups(pool, experiment_hash)
    groups = sorted(set(model_group_ids)) if model_group_ids else in_experiment
    unknown = [g for g in groups if g not in in_experiment]
    if unknown:
        raise ValueError(
            f"model group(s) {', '.join(map(str, unknown))} have no model in experiment"
            f" {experiment_hash[:12]} — list its groups with: triage models"
            f" {experiment_hash[:12]}"
        )
    if len(groups) < 2:
        raise ValueError(
            f"comparing on common rows needs two or more model groups, got {groups}"
        )
    return groups


def evaluate_on_common_rows(
    pool: DictRowPool,
    experiment_hash: str,
    model_group_ids: Optional[Sequence[int]] = None,
    metric_config: Optional[dict[str, Any]] = None,
) -> CommonRows:
    """Register the common rows of the groups as a Subset and evaluate their models on it.

    Args:
        pool: the project database.
        experiment_hash: the full experiment hash.
        model_group_ids: the groups to compare; ``None`` takes every group of the Experiment.
        metric_config: the metric set; ``None`` resolves the Experiment's, as a run does.

    Returns:
        The Subset's hash and name, the groups, its member rows and dates, and the number of
        evaluation rows written.

    Raises:
        ValueError: fewer than two groups, or a group with no model in the Experiment.
    """
    from triage.adapters.run import _resolve_metric_config

    groups = compared_groups(pool, experiment_hash, model_group_ids)

    with pool.connection() as conn:
        experiment = returned_row(
            conn.execute(
                "select config, problem_type::text as problem_type"
                " from triage.experiments where experiment_hash = %(h)s",
                {"h": experiment_hash},
            ).fetchone()
        )
        subset_hash = returned_row(
            conn.execute(
                "select triage.materialize_common_rows(%(g)s, 'test') as h",
                {"g": groups},
            ).fetchone()
        )["h"]
        size = returned_row(
            conn.execute(
                "select count(*) as n, count(distinct as_of_date) as d,"
                "       (select config->>'name' from triage.subsets"
                "         where subset_hash = %(h)s) as name"
                " from triage.subset_members where subset_hash = %(h)s",
                {"h": subset_hash},
            ).fetchone()
        )
        # each model's scored dates that hold common rows, at that date's label horizon
        targets = conn.execute(
            "select m.model_id, ls.as_of_date, ls.label_timespan::text as label_timespan"
            " from triage.models m"
            " join triage.runs r on r.run_id = m.run_id"
            " cross join lateral triage.label_source(m.model_id, 'test') ls"
            " where r.experiment_hash = %(h)s and m.model_group_id = any(%(g)s)"
            "   and exists (select 1 from triage.subset_members sm"
            "                where sm.subset_hash = %(s)s"
            "                  and sm.as_of_date = ls.as_of_date)"
            " order by m.model_id, ls.as_of_date",
            {"h": experiment_hash, "g": groups, "s": subset_hash},
        ).fetchall()

    config = metric_config or _resolve_metric_config(
        experiment["config"] or {}, experiment["problem_type"], None
    )
    written = sum(
        evaluate_in_db(
            pool,
            t["model_id"],
            t["as_of_date"],
            t["label_timespan"],
            metric_config=config,
            subset_hash=subset_hash,
        )
        for t in targets
    )
    return CommonRows(
        subset_hash=subset_hash,
        name=size["name"],
        model_group_ids=groups,
        n_rows=size["n"],
        n_dates=size["d"],
        n_evaluations=written,
    )
