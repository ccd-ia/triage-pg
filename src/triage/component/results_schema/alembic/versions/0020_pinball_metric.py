"""pinball / quantile-loss regression metric (v1.0.1, Phase 5)

Adds ``pinball@<tau>`` to the in-PG regression metrics (ADR-0007: metrics are PL/pgSQL over the
predictions/labels), so audition can honestly rank the *quantile* forecasts the new baselines
emit (``DummyRegressor(strategy='quantile')``, Croston, ETS intervals) — RMSE/MAE only score a
point forecast. Pinball is a loss (lower is better), so ``higher_is_better`` is taught to treat
``pinball@%`` like rmse/mae; the ``triage.metric_catalog`` view + audition selection then pick it
up automatically (no metric_catalog change — it is a view over evaluations + higher_is_better).

The metric is stored with the quantile in the metric name (``metric='pinball@0.9'``,
``parameter=''``), so ``evaluate_model``'s existing regression loop needs no change — only
``regression_metric`` (the pinball branch) and ``higher_is_better`` (the direction).

Revision ID: 0020_pinball_metric
Revises: 0019_task_framing
"""

from alembic import op

revision = "0020_pinball_metric"
down_revision = "0019_task_framing"
branch_labels = None
depends_on = None


# --------------------------------------------------------------------------- upgrade DDL

HIGHER_IS_BETTER_PINBALL = r"""
create or replace function triage.higher_is_better(metric text)
returns boolean language sql immutable as $$
  select metric is null
      or (metric not in ('rmse', 'mae',
                         'false positives@', 'false negatives@', 'fpr@')
          and metric not like 'pinball@%');
$$;
"""

REGRESSION_METRIC_PINBALL = r"""
create or replace function triage.regression_metric(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric         text,          -- 'rmse' | 'mae' | 'r2' | 'pinball@<tau>'
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    mean_y      double precision;
    ss_res      double precision;
    ss_tot      double precision;
    v_tau       double precision;
begin
    select count(*)::int, avg(outcome)
      into n_labeled, mean_y
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);

    r.num_labeled := n_labeled;
    if n_labeled = 0 then
        return r;
    end if;

    if p_metric = 'rmse' then
        select sqrt(avg(power(score - outcome, 2)))
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);
    elsif p_metric = 'mae' then
        select avg(abs(score - outcome))
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);
    elsif p_metric = 'r2' then
        select sum(power(outcome - score, 2)),
               sum(power(outcome - mean_y, 2))
          into ss_res, ss_tot
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);
        if ss_tot = 0 then
            r.value := null;  -- R² undefined when the target is constant
        else
            r.value := 1 - ss_res / ss_tot;
        end if;
    elsif p_metric like 'pinball@%' then
        -- Pinball (quantile) loss for a τ-quantile forecast: the score IS ŷ_τ, outcome is y.
        --   L_τ = mean( τ·(y−ŷ)   when y ≥ ŷ,   (1−τ)·(ŷ−y)  when y < ŷ )
        -- Lower is better (higher_is_better handles 'pinball@%').
        v_tau := split_part(p_metric, '@', 2)::double precision;
        if v_tau <= 0 or v_tau >= 1 then
            raise exception 'pinball quantile must be in (0,1), got %', p_metric;
        end if;
        select avg(case when outcome >= score
                        then v_tau * (outcome - score)
                        else (1 - v_tau) * (score - outcome) end)
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);
    else
        raise exception 'unknown regression metric % (expected rmse|mae|r2|pinball@<tau>)', p_metric;
    end if;

    return r;
end;
$$;
"""


# ------------------------------------------------------------------------- downgrade DDL
# Restore the 0004 higher_is_better + the 0015 regression_metric (no pinball).

HIGHER_IS_BETTER_0004 = r"""
create or replace function triage.higher_is_better(metric text)
returns boolean language sql immutable as $$
  select metric is null
      or metric not in ('rmse', 'mae',
                        'false positives@', 'false negatives@', 'fpr@');
$$;
"""

REGRESSION_METRIC_0015 = r"""
create or replace function triage.regression_metric(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric         text,
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    mean_y      double precision;
    ss_res      double precision;
    ss_tot      double precision;
begin
    select count(*)::int, avg(outcome)
      into n_labeled, mean_y
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);

    r.num_labeled := n_labeled;
    if n_labeled = 0 then
        return r;
    end if;

    if p_metric = 'rmse' then
        select sqrt(avg(power(score - outcome, 2)))
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);
    elsif p_metric = 'mae' then
        select avg(abs(score - outcome))
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);
    elsif p_metric = 'r2' then
        select sum(power(outcome - score, 2)),
               sum(power(outcome - mean_y, 2))
          into ss_res, ss_tot
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);
        if ss_tot = 0 then
            r.value := null;
        else
            r.value := 1 - ss_res / ss_tot;
        end if;
    else
        raise exception 'unknown regression metric % (expected rmse|mae|r2)', p_metric;
    end if;

    return r;
end;
$$;
"""


def upgrade() -> None:
    op.execute(HIGHER_IS_BETTER_PINBALL)
    op.execute(REGRESSION_METRIC_PINBALL)


def downgrade() -> None:
    op.execute(REGRESSION_METRIC_0015)
    op.execute(HIGHER_IS_BETTER_0004)
