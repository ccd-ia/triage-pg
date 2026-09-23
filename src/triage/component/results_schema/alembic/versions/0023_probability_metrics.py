"""probability metrics and registered metric directions (#6)

Adds three classification metrics that score the *probability* a model emits, not only its
ranking (ADR-0007: metrics are PL/pgSQL over ``labeled_ranks``):

* ``brier`` — mean of ``(p − y)²``; ``sklearn.metrics.brier_score_loss`` for a binary label.
* ``log_loss`` — mean of ``−(y·ln p + (1−y)·ln(1−p))`` with ``p`` clipped to ``[ε, 1−ε]``,
  ``ε`` the float64 machine epsilon, which is what ``sklearn.metrics.log_loss`` does.
* ``ece@<bins>`` — expected calibration error over ``bins`` equal-width bins of ``[0, 1]``:
  ``Σ_b |Σ y − Σ p| / N``. Bins are ``sklearn.calibration.calibration_curve``'s
  (``strategy='uniform'``): a score on an inner edge falls in the lower bin, 1.0 in the last.

``y`` is ``outcome > 0``, the same positive the top-k metrics count. The metrics are stored like
``pinball@τ``: the bin count lives in the metric name (``metric='ece@10'``, ``parameter=''``).
All three are losses (lower is better). A score outside ``[0, 1]`` is not a probability — a
``decision_function`` margin, a heuristic ranker's raw column — and the value is NULL, the way
``r2`` is NULL on a constant target, so one non-probabilistic estimator in a grid does not fail
the run.

**Registered directions.** ``higher_is_better`` treated any metric it did not know as
higher-is-better, so audition and the leaderboards ranked a custom loss backwards with no error.
``triage.metric_directions`` is where a project records a metric's direction; the function reads
it before its built-in list. A row matches its exact metric name, or — when it ends in ``@`` —
every metric of that family (``'ece@'`` covers ``ece@10`` and ``ece@15``); the longest match
wins. An unregistered, unknown metric keeps the old default. Reading a table makes the function
``stable`` instead of ``immutable``; nothing indexes, generates or constrains on it, which are
the uses that would require ``immutable``.

Replacing ``higher_is_better`` overwrites a project's own edit of that function. Such an edit
belongs in ``triage.metric_directions`` now.

Revision ID: 0023_probability_metrics
Revises: 0022_audition_window
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "0023_probability_metrics"
down_revision = "0022_audition_window"
branch_labels = None
depends_on = None


def _revision_ddl(module: str, name: str) -> str:
    """A DDL constant of an earlier revision, read from that file rather than retyped.

    Revision files start with a digit and are not importable by name; loading one by path keeps
    this migration's derived and restored bodies identical to what that revision created.
    """
    path = Path(__file__).with_name(f"{module}.py")
    spec = importlib.util.spec_from_file_location(f"_triage_rev_{module}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return getattr(loaded, name)


# --------------------------------------------------------------------------- upgrade DDL

METRIC_DIRECTIONS_DDL = r"""
create table triage.metric_directions (
    metric           text primary key check (metric <> ''),
    higher_is_better boolean not null,
    note             text,
    registered_at    timestamptz not null default now()
);
comment on table triage.metric_directions is
    'Direction of a metric for audition and the leaderboards. A row matches its exact metric '
    'name, or every metric of a family when it ends in @ (ece@ covers ece@10). The longest '
    'match wins, and a registered row overrides the built-in list in triage.higher_is_better.';
"""

HIGHER_IS_BETTER_REGISTERED = r"""
create or replace function triage.higher_is_better(metric text)
returns boolean language sql stable as $$
  select coalesce(
    (select d.higher_is_better
       from triage.metric_directions d
      where d.metric = $1
         or (right(d.metric, 1) = '@' and starts_with($1, d.metric))
      order by length(d.metric) desc
      limit 1),
    $1 is null
    or ($1 not in ('rmse', 'mae', 'brier', 'log_loss',
                   'false positives@', 'false negatives@', 'fpr@')
        and $1 not like 'pinball@%'
        and $1 not like 'ece@%'));
$$;
"""

PROBABILITY_METRIC_DDL = r"""
create or replace function triage.probability_metric(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric         text,          -- 'brier' | 'log_loss' | 'ece@<bins>'
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r          triage.metric_result;
    n_outside  integer;
    v_bins     integer;
    -- float64 machine epsilon: sklearn clips log_loss's p to [eps, 1 - eps]
    v_eps      constant double precision := 2.220446049250313e-16;
begin
    if p_metric like 'ece@%' then
        if split_part(p_metric, '@', 2) !~ '^[0-9]+$'
           or split_part(p_metric, '@', 2)::integer < 1 then
            raise exception 'ece needs a positive whole number of bins, got % (e.g. ece@10)',
                p_metric;
        end if;
        v_bins := split_part(p_metric, '@', 2)::integer;
    elsif p_metric not in ('brier', 'log_loss') then
        raise exception 'unknown probability metric % (expected brier|log_loss|ece@<bins>)',
            p_metric;
    end if;

    select count(*)::int,
           coalesce(sum((outcome > 0)::int), 0)::int,
           count(*) filter (where score < 0 or score > 1)::int
      into r.num_labeled, r.num_positive, n_outside
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                p_subset_hash);

    -- No labeled rows, or scores that are not probabilities: the value is undefined.
    if r.num_labeled = 0 or n_outside > 0 then
        return r;
    end if;

    if p_metric = 'brier' then
        select avg(power(score - (outcome > 0)::int, 2))
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                    p_subset_hash);
    elsif p_metric = 'log_loss' then
        select -avg(case when outcome > 0 then ln(greatest(least(score, 1 - v_eps), v_eps))
                         else ln(1 - greatest(least(score, 1 - v_eps), v_eps)) end)
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                    p_subset_hash);
    else
        -- calibration_curve's bin: the number of inner edges i/bins (i = 1..bins-1, the
        -- same float products numpy.linspace makes) strictly below the score.
        select sum(abs(b.y - b.p)) / r.num_labeled
          into r.value
          from (select sum((lr.outcome > 0)::int)::double precision as y,
                       sum(lr.score)                                  as p
                  from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date,
                                            p_label_timespan, p_subset_hash) lr
                  cross join lateral (
                      select count(*) as bin
                        from generate_series(1, v_bins - 1) as g(i)
                       where g.i * (1.0::double precision / v_bins) < lr.score) e
                 group by e.bin) b;
    end if;

    return r;
end;
$$;
"""

# evaluate_model: 0015's body with one more classification branch, inserted before the
# unknown-metric error. Everything else is that revision's text.
_UNKNOWN_CLASSIFICATION = (
    "        else\n"
    "            raise exception 'unknown classification metric % (expected "
    "precision@|recall@|auc_roc|average_precision)', v_metric;\n"
)

_PROBABILITY_BRANCH = r"""        elsif v_metric in ('brier', 'log_loss') or v_metric like 'ece@%' then
            res := triage.probability_metric(p_model_id, p_split_kind, p_as_of_date,
                                             p_label_timespan, v_metric, p_subset_hash);
            insert into triage.evaluations (
                model_id, split_kind, as_of_date, subset_hash, metric, parameter,
                value, value_worst, value_best, value_expected, value_std,
                num_labeled, num_positive, computed_at)
            values (
                p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, '',
                res.value, null, null, null, null,
                res.num_labeled, res.num_positive, now())
            on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
            do update set value = excluded.value,
                          value_worst = null, value_best = null,
                          value_expected = null, value_std = null,
                          num_labeled = excluded.num_labeled,
                          num_positive = excluded.num_positive,
                          computed_at = excluded.computed_at;
            written := written + 1;
        else
            raise exception 'unknown classification metric % (expected precision@|recall@|auc_roc|average_precision|brier|log_loss|ece@<bins>)', v_metric;
"""


def _evaluate_model_0015() -> str:
    return _revision_ddl("0015_subset_evaluation", "EVALUATE_MODEL_DDL")


def _evaluate_model_probability() -> str:
    body = _evaluate_model_0015()
    if body.count(_UNKNOWN_CLASSIFICATION) != 1:
        raise RuntimeError(
            "0015's evaluate_model no longer has exactly one unknown-classification branch;"
            " 0023 cannot insert the probability metrics into it"
        )
    return body.replace(_UNKNOWN_CLASSIFICATION, _PROBABILITY_BRANCH)


def upgrade() -> None:
    op.execute(METRIC_DIRECTIONS_DDL)
    op.execute(HIGHER_IS_BETTER_REGISTERED)
    op.execute(PROBABILITY_METRIC_DDL)
    op.execute(_evaluate_model_probability())


def downgrade() -> None:
    op.execute(_evaluate_model_0015())
    op.execute(
        "drop function if exists triage.probability_metric("
        "bigint, triage.split_kind, date, interval, text, text)"
    )
    op.execute(_revision_ddl("0020_pinball_metric", "HIGHER_IS_BETTER_PINBALL"))
    op.execute("drop table if exists triage.metric_directions")
