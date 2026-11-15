"""the threshold curve counts a positive as outcome > 0, as precision@ and recall@ do

``model_threshold_curve`` (0028) took ``sum(outcome)`` as its count of positives. That is the
count only for a 0/1 label. On a ``regression_ranking`` target the sum of the outcomes in a cut
exceeds the labeled rows there, so FP and TN went negative, and precision, recall and the base
rate were outcome means instead of shares. The error-tree reference rates read the same counts.

The curve now counts ``outcome > 0``, the positive ``precision_at_k``, ``recall_at_k`` and the
near-miss bands read: at every k its precision and recall are the stored ``precision@k_abs``
and ``recall@k_abs``, and its base rate is their random-ranking expectation. On a 0/1 label
``outcome > 0`` is the outcome, so a classifier's curve is unchanged, row for row.

The signature and the output are 0028's, so the function is replaced in place. Downgrade
restores 0028's text.

Revision ID: 0038_curve_positive_outcome
Revises: 0037_near_miss_and_prototypes
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0038_curve_positive_outcome"
down_revision = "0037_near_miss_and_prototypes"
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


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(
            f"anchor {old[:60]!r} occurs {text.count(old)} times, expected once"
        )
    return text.replace(old, new)


def _curve_0028() -> str:
    """0028's three-argument curve, without the drop that precedes it, as a replace."""
    rev_0028 = _revision_module("0028_subset_threshold_curve")
    text = rev_0028._from(rev_0028.CURVE_DDL, "-- The Rayid curve of one test date:")
    return _replace_once(
        text,
        "create function triage.model_threshold_curve(",
        "create or replace function triage.model_threshold_curve(",
    )


# The positives of the population and of each cut. A double precision count keeps the
# divisions below them what they were for a 0/1 label (and not integer division).
_TOTAL_POSITIVES = (
    "        select count(*) as n, count(ranked.outcome) as l,\n"
    "               coalesce(sum(ranked.outcome), 0) as p\n"
)
_TOTAL_POSITIVES_0038 = (
    "        -- a positive is outcome > 0, as precision@ and recall@ read it: a\n"
    "        -- classifier's 1, a regression_ranking target above zero\n"
    "        select count(*) as n, count(ranked.outcome) as l,\n"
    "               (count(*) filter (where ranked.outcome > 0))::double precision as p\n"
)
_CUT_POSITIVES = (
    "               coalesce(sum(ranked.outcome) over (order by ranked.rank_abs), 0)"
    " as tp,\n"
)
_CUT_POSITIVES_0038 = (
    "               (count(*) filter (where ranked.outcome > 0)\n"
    "                    over (order by ranked.rank_abs))::double precision         as tp,\n"
)


def _curve_0038() -> str:
    """0028's curve with ``outcome > 0`` counted where it summed the outcome."""
    body = _replace_once(_curve_0028(), _TOTAL_POSITIVES, _TOTAL_POSITIVES_0038)
    return _replace_once(body, _CUT_POSITIVES, _CUT_POSITIVES_0038)


def upgrade() -> None:
    op.execute(_curve_0038())


def downgrade() -> None:
    op.execute(_curve_0028())
