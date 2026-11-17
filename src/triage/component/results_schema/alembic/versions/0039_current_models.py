"""one model per group and split: group-level reads take the latest Run's model (#69)

A model group is the estimator, its hyperparameters and its feature list. A model is more:
it also covers the train matrix, the seed and the engine versions. So a second Run of an
Experiment that changes only those inputs (a triage-pg upgrade, a ``history_query``, an
imputation strategy, a new data snapshot) fits a new model inside each existing group, on
the same splits. The group then held two models per test date. The common-rows functions
refused that state, and audition counted every model as a split and averaged the
attempts.

``triage.current_models`` keeps one model per Experiment, group, train end, label
timespan and forecast quantile: the one from the latest Run that built or reused it. A
Run that completed outranks one that is still running or failed, so a re-run in progress
does not replace the previous attempt split by split, and a failed one never does.
``triage.run_artifacts`` records the building Run and every Run that reused the model.

The readers that aggregate a group over its dates read the view: the audition
distances (and so ``audition``, ``audition_windowed`` and the picks), the leaderboard,
``model_group_summary``, ``scored_coverage`` (and so ``common_rows_metrics`` and
``model_group_coverage``), ``materialize_common_rows``, ``entity_score_history``,
``error_rule_recurrence`` and ``latest_model``. Per-model reads, lineage, the bias rows,
the monitoring windows and ``experiment_actuals`` (a count of what was built) still read
``triage.models``. No signature or column changes. Downgrade restores every prior text
and drops the view.

Revision ID: 0039_current_models
Revises: 0038_curve_positive_outcome
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0039_current_models"
down_revision = "0038_curve_positive_outcome"
branch_labels = None
depends_on = None


CURRENT_MODELS_DDL = r"""
-- One model per (Experiment, group, train end, label timespan, forecast quantile): the one
-- from the latest Run of the Experiment that built or reused it. A completed Run outranks a
-- started or failed one. A model belongs to the Experiment of the Run that built it
-- (models.run_id); a later Run that cache-hits it is recorded in run_artifacts only.
create view triage.current_models as
with used_by as (
    select m.model_id, m.run_id from triage.models m where m.run_id is not null
    union
    select m.model_id, ra.run_id
    from   triage.models m
    join   triage.run_artifacts ra on ra.artifact_id = m.model_hash
),
uses as (
    select m.model_id, own.experiment_hash,
           max(r.started_at) filter (where r.status = 'completed') as last_completed_at,
           max(r.started_at)                                       as last_used_at
    from   triage.models m
    join   triage.runs own on own.run_id     = m.run_id
    join   used_by     ub  on ub.model_id    = m.model_id
    join   triage.runs r   on r.run_id       = ub.run_id
                          and r.experiment_hash = own.experiment_hash
    group by m.model_id, own.experiment_hash
)
-- A model with no Run (its run row deleted, run_id set null) has no Experiment: it is kept
-- with a null experiment_hash, so the functions keyed by model group still read it.
select distinct on (u.experiment_hash, m.model_group_id, m.train_end_time,
                    m.training_label_timespan, m.forecast_quantile)
       m.*, u.experiment_hash
from   triage.models m
left join uses u on u.model_id = m.model_id
order by u.experiment_hash, m.model_group_id, m.train_end_time,
         m.training_label_timespan, m.forecast_quantile,
         (u.last_completed_at is not null) desc,
         coalesce(u.last_completed_at, u.last_used_at) desc,
         m.model_id desc;
"""


def _revision_module(module: str) -> ModuleType:
    """An earlier revision, loaded from its file (revision files are not importable by name)."""
    path = Path(__file__).with_name(f"{module}.py")
    spec = importlib.util.spec_from_file_location(f"_triage_rev_{module}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _replace(text: str, old: str, new: str, count: int) -> str:
    """``text`` with ``old`` replaced by ``new``; ``old`` must occur exactly ``count`` times."""
    if text.count(old) != count:
        raise RuntimeError(
            f"{old[:60]!r} occurs {text.count(old)} times, expected {count}"
        )
    return text.replace(old, new)


def _block(module: str, constant: str, start: str, end: str) -> str:
    """The one statement of ``module.constant`` from ``start`` through the first ``end``."""
    text = getattr(_revision_module(module), constant)
    if text.count(start) != 1:
        raise RuntimeError(
            f"{start!r} occurs {text.count(start)} times in {module}.{constant}"
        )
    i = text.index(start)
    return text[i : text.index(end, i) + len(end)]


_FUNCTION_END = "\n$$;\n"

# (module, constant, start, end, occurrences of "triage.models" in the statement)
_PREVIOUS = (
    (
        "0031_common_rows_and_coverage",
        "DISTANCES_BY_SUBSET_DDL",
        "create view triage.audition_distances_by_subset as",
        "from base;\n",
        1,
    ),
    (
        "0005_experiment_dashboard",
        "UPGRADE_DDL",
        "create or replace view triage.model_group_summary as",
        "mg.hyperparameters, mg.feature_list;\n",
        1,
    ),
    (
        "0005_experiment_dashboard",
        "UPGRADE_DDL",
        "create or replace function triage.latest_model(",
        _FUNCTION_END,
        1,
    ),
    (
        "0031_common_rows_and_coverage",
        "COMMON_ROWS_DDL",
        "create function triage.scored_coverage(",
        _FUNCTION_END,
        1,
    ),
    (
        "0031_common_rows_and_coverage",
        "COMMON_ROWS_DDL",
        "create function triage.materialize_common_rows(",
        _FUNCTION_END,
        1,
    ),
    (
        "0006_dashboard_drilldowns",
        "UPGRADE_DDL",
        "create or replace function triage.entity_score_history(",
        _FUNCTION_END,
        1,
    ),
    (
        "0027_error_confusion_recurrence",
        "UPGRADE_DDL",
        "create function triage.error_rule_recurrence(",
        _FUNCTION_END,
        1,
    ),
)


def _as_replace(statement: str) -> str:
    """A ``create view`` / ``create function`` statement as its ``or replace`` form."""
    for kind in ("view", "function"):
        if statement.startswith(f"create {kind} "):
            return f"create or replace {kind} " + statement[len(f"create {kind} ") :]
    return statement


def _previous() -> list[tuple[str, int]]:
    """Each object's text as it stands at 0038, with its count of ``triage.models``."""
    return [
        (_as_replace(_block(module, constant, start, end)), count)
        for module, constant, start, end, count in _PREVIOUS
    ]


def _leaderboard_0015() -> str:
    """0015's leaderboard: drop, create with no data, refresh."""
    return _revision_module("0015_subset_evaluation").LEADERBOARD_DDL


def upgrade() -> None:
    op.execute(CURRENT_MODELS_DDL)
    for statement, count in _previous():
        op.execute(
            _replace(statement, "triage.models ", "triage.current_models ", count)
        )
    op.execute(
        _replace(_leaderboard_0015(), "triage.models ", "triage.current_models ", 1)
    )


def downgrade() -> None:
    op.execute(_leaderboard_0015())
    for statement, _count in _previous():
        op.execute(statement)
    op.execute("drop view triage.current_models")
