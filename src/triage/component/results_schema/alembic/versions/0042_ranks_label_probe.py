"""the ranks functions look up each scored row's label by index, whatever the statistics say (#102)

``population_ranks`` (0024), ``labeled_ranks`` and ``survival_ranks`` (0015) joined the
model's predictions to ``triage.labels`` as a plain join. When the statistics had not yet
seen the run's labels artifact, its label timespan or the model's predictions, the planner
estimated about one row on each side. It then chose a nested loop whose inner side scans
``labels_pkey`` on ``label_hash`` and ``label_timespan`` only, and checks entity and date as
a join filter. That reads every label of the artifact once per prediction: 96 minutes for
one model with 16,872 predictions and 138,803 labels, in place of 22 ms.

Each function now reads the label through a correlated subquery with ``offset 0``. The
offset stops the planner from flattening the subquery into a join, so the lookup always
runs once per scored row with the entity, the date and the timespan as index keys. The
result rows and their order are unchanged, and two label rows that match one prediction
(no labels lineage) still produce two output rows.

Downgrade restores the 0024 and 0015 bodies.

Revision ID: 0042_ranks_label_probe
Revises: 0041_entity_attributes_scoped
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0042_ranks_label_probe"
down_revision = "0041_entity_attributes_scoped"
branch_labels = None
depends_on = None


def _revision_module(module: str) -> ModuleType:
    """An earlier revision, loaded from its file (revision files are not importable by name)."""
    path = Path(__file__).with_name(f"{module}.py")
    spec = importlib.util.spec_from_file_location(f"_triage_rev_{module}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _between(text: str, start: str, end: str | None = None) -> str:
    """The slice of ``text`` from ``start`` up to ``end`` (or the end), each found exactly once."""
    for anchor in (start, end):
        if anchor is not None and text.count(anchor) != 1:
            raise RuntimeError(
                f"anchor {anchor[:60]!r} occurs {text.count(anchor)} times, expected once"
            )
    lo = text.index(start)
    return text[lo : text.index(end) if end is not None else len(text)]


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(
            f"anchor {old[:60]!r} occurs {text.count(old)} times, expected once"
        )
    return text.replace(old, new)


# ------------------------------------------------------------------------- old bodies


def _population_ranks_0024() -> str:
    body = _between(
        _revision_module("0024_population_topk").POPULATION_RANKS_DDL,
        "create function triage.population_ranks(",
        "-- Standard deviation of precision@k",
    )
    return _replace_once(
        body,
        "create function triage.population_ranks(",
        "create or replace function triage.population_ranks(",
    )


def _ranks_0015() -> str:
    return _revision_module("0015_subset_evaluation").RANKS_DDL


def _labeled_ranks_0015() -> str:
    return _between(
        _ranks_0015(),
        "create or replace function triage.labeled_ranks(",
        "drop function if exists triage.survival_ranks(",
    )


def _survival_ranks_0015() -> str:
    return _between(_ranks_0015(), "create or replace function triage.survival_ranks(")


# ------------------------------------------------------------------------- new bodies

# The pinned label lookup, once per scored row. offset 0 keeps the subquery correlated: the
# planner cannot flatten it into a join, so a wrong row estimate cannot turn the lookup into
# a scan of the whole labels artifact per prediction (#102).
_PROBE = """lateral (
           select {columns}
           from   triage.labels l
           where  l.entity_id      = {row}.entity_id
             and  l.as_of_date     = {row}.as_of_date
             and  l.label_timespan = p_label_timespan
             and  (v_pin is null or l.label_hash = v_pin)
           offset 0) l"""

_INNER_JOIN = """    join   triage.labels l
           on  l.entity_id      = p.entity_id
           and l.as_of_date     = p.as_of_date
           and l.label_timespan = p_label_timespan
           and (v_pin is null or l.label_hash = v_pin)
"""


def _population_ranks_0042() -> str:
    old = """    left join triage.labels l
           on  l.entity_id      = pop.entity_id
           and l.as_of_date     = pop.as_of_date
           and l.label_timespan = p_label_timespan
           and (v_pin is null or l.label_hash = v_pin);"""
    new = "    left join " + _PROBE.format(columns="l.outcome", row="pop") + " on true;"
    return _replace_once(_population_ranks_0024(), old, new)


def _labeled_ranks_0042() -> str:
    new = "    cross join " + _PROBE.format(columns="l.outcome", row="p") + "\n"
    return _replace_once(_labeled_ranks_0015(), _INNER_JOIN, new)


def _survival_ranks_0042() -> str:
    new = (
        "    cross join "
        + _PROBE.format(columns="l.duration, l.event_observed", row="p")
        + "\n"
    )
    return _replace_once(_survival_ranks_0015(), _INNER_JOIN, new)


def upgrade() -> None:
    op.execute(_population_ranks_0042())
    op.execute(_labeled_ranks_0042())
    op.execute(_survival_ranks_0042())


def downgrade() -> None:
    op.execute(_population_ranks_0024())
    op.execute(_labeled_ranks_0015())
    op.execute(_survival_ranks_0015())
