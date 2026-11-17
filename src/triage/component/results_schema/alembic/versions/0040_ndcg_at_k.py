"""ndcg@k: the order inside the top k, next to lift@k and captured@k (#83)

lift@k and captured@k treat the top k as a set, so two models that select the same k rows
score the same whatever order they put them in. A program that works its list from the top
and may stop part of the way through needs the order too. ``ndcg@<k>`` scores it:

* DCG@k sums each labeled outcome inside the population cut divided by log2(rank + 1),
  over the population ranking ``allocation_at_k`` already reads (every scored row ranked,
  ties broken by entity_id, ADR-0033). An unlabeled row keeps its position and adds nothing.
* IDCG@k is the same sum with the labeled outcomes sorted in descending order.
* NDCG@k = DCG@k / IDCG@k, between 0 and 1. The gain is the outcome itself (linear
  gains): 2^outcome - 1 overflows for counts and amounts. On a 0/1 label it is the label.

It follows lift@ and captured@'s rule for an undefined share: NULL when a labeled outcome is
negative (IDCG then no longer bounds DCG) or every labeled outcome is zero (IDCG = 0).

``allocation_at_k`` and ``evaluate_model`` keep their signatures and are replaced in place;
any problem type may list ``ndcg@`` in ``metrics``. Downgrade deletes the ndcg@ rows and
restores 0035's text.

Revision ID: 0040_ndcg_at_k
Revises: 0039_current_models
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0040_ndcg_at_k"
down_revision = "0039_current_models"
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


_REV_0035 = "0035_allocation_metrics"


def _allocation_0035() -> str:
    """0035's allocation_at_k alone, as a replace."""
    text = _revision_module(_REV_0035).ALLOCATION_DDL
    start = "create function triage.allocation_at_k("
    i = text.index(start)
    block = text[i : text.index("\n$$;\n", i) + len("\n$$;\n")]
    return _replace_once(
        block, start, "create or replace function triage.allocation_at_k("
    )


_CHECK = (
    "    if p_metric not in ('lift@', 'captured@') then\n"
    "        raise exception 'unknown allocation metric % (expected lift@|captured@)',"
    " p_metric;\n"
)
_CHECK_0040 = (
    "    if p_metric not in ('lift@', 'captured@', 'ndcg@') then\n"
    "        raise exception"
    " 'unknown allocation metric % (expected lift@|captured@|ndcg@)', p_metric;\n"
)
_DECLARE = "    top_sum    double precision;\n"
_DECLARE_0040 = (
    "    top_sum    double precision;\n"
    "    dcg        double precision;\n"
    "    idcg       double precision;\n"
)
_SHARES = "    if p_metric = 'captured@' then\n"
_SHARES_0040 = (
    "    if p_metric = 'ndcg@' then\n"
    "        -- the gain of a labeled row at population rank i is its outcome / log2(i + 1)\n"
    "        select coalesce(sum(outcome / (ln(rank_abs + 1.0) / ln(2.0))), 0)\n"
    "          into dcg\n"
    "          from triage.population_ranks(p_model_id, p_split_kind, p_as_of_date,\n"
    "                                       p_label_timespan, p_subset_hash)\n"
    "         where rank_abs <= k and outcome is not null;\n"
    "        -- the ideal order: the labeled outcomes, largest first\n"
    "        select coalesce(sum(o / (ln(i + 1.0) / ln(2.0))), 0)\n"
    "          into idcg\n"
    "          from (select outcome as o, row_number() over (order by outcome desc) as i\n"
    "                  from triage.population_ranks(p_model_id, p_split_kind, p_as_of_date,\n"
    "                                               p_label_timespan, p_subset_hash)\n"
    "                 where outcome is not null) ideal\n"
    "         where i <= k;\n"
    "        if idcg > 0 then\n"
    "            r.value := dcg / idcg;\n"
    "        end if;\n"
    "        return r;\n"
    "    end if;\n"
    "    if p_metric = 'captured@' then\n"
)


def _allocation_0040() -> str:
    body = _replace_once(_allocation_0035(), _CHECK, _CHECK_0040)
    body = _replace_once(body, _DECLARE, _DECLARE_0040)
    return _replace_once(body, _SHARES, _SHARES_0040)


def _evaluate_model_0035() -> str:
    return _revision_module(_REV_0035)._evaluate_model_0035()


def _evaluate_model_0040() -> str:
    """0035's evaluate_model with ndcg@ routed to allocation_at_k."""
    body = _replace_once(
        _evaluate_model_0035(),
        "        if v_metric in ('precision@', 'recall@', 'lift@', 'captured@') then\n",
        "        if v_metric in ('precision@', 'recall@', 'lift@', 'captured@', 'ndcg@')"
        " then\n",
    )
    return _replace_once(
        body,
        "(expected precision@|recall@|lift@|captured@|auc_roc|",
        "(expected precision@|recall@|lift@|captured@|ndcg@|auc_roc|",
    )


def upgrade() -> None:
    op.execute(_allocation_0040())
    op.execute(_evaluate_model_0040())


def downgrade() -> None:
    op.execute("delete from triage.evaluations where metric = 'ndcg@';")
    op.execute(_evaluate_model_0035())
    op.execute(_allocation_0035())
    op.execute("refresh materialized view triage.leaderboard;")
