"""WAPE, and pinball@τ only for the model that forecasts τ

``pinball@τ`` scores a τ-quantile forecast. 0020 computed it for every regression model, so a
point forecast (a mean) and a 0.9-quantile baseline got a pinball@0.5, a pinball@0.8 and a
pinball@0.95 each, and audition could pick a point model on a quantile loss it never aimed at.

* ``triage.models.forecast_quantile`` records the quantile a model forecasts, or NULL for a point
  forecast. Training writes it (``adapters.model.declared_quantile``); this revision backfills it
  from ``model_groups`` for the sklearn and LightGBM quantile estimators.
* ``regression_metric``'s pinball branch returns NULL unless ``|forecast_quantile − τ| < 1e-9``.
  The row is still written, with its ``num_labeled``, so a heatmap shows "not applicable" rather
  than a missing cell: the way ``ece@`` is NULL for a score outside ``[0, 1]`` (0023).
* ``wape`` — ``Σ|ŷ − y| / Σ|y|``, NULL when ``Σ|y| = 0``. A loss: ``higher_is_better`` lists it
  with rmse and mae.

The backfill covers ``DummyRegressor`` (strategy ``quantile`` → its ``quantile``; ``median`` →
0.5), ``GradientBoostingRegressor`` and ``LGBMRegressor`` (a quantile loss → ``alpha``, default
0.9), ``HistGradientBoostingRegressor`` (loss ``quantile`` → its ``quantile``) and
``QuantileRegressor`` (``quantile``, default 0.5). Stored pinball values are not recomputed here:
re-evaluating a model, or ``triage check metrics``, shows the new NULLs.

Downgrade drops the column and restores the 0020 ``regression_metric`` and the 0023
``higher_is_better``.

Revision ID: 0030_wape_and_forecast_quantile
Revises: 0029_typed_calibration
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "0030_wape_and_forecast_quantile"
down_revision = "0029_typed_calibration"
branch_labels = None
depends_on = None


def _revision_ddl(module: str, name: str) -> str:
    """A DDL constant of an earlier revision, read from that file rather than retyped."""
    path = Path(__file__).with_name(f"{module}.py")
    spec = importlib.util.spec_from_file_location(f"_triage_rev_{module}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration {path}")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return getattr(loaded, name)


# --------------------------------------------------------------------------- upgrade DDL

FORECAST_QUANTILE_DDL = r"""
alter table triage.models
    add column forecast_quantile double precision
        check (forecast_quantile >= 0 and forecast_quantile <= 1);
comment on column triage.models.forecast_quantile is
    'The quantile this model forecasts (0.9 for a 0.9-quantile regressor), or NULL for a point '
    'forecast. pinball@tau is computed only where tau equals it.';

-- The quantile estimators by their class name; the same table as
-- triage.adapters.model.QUANTILE_ESTIMATORS.
update triage.models m
   set forecast_quantile = q.quantile
  from (select mg.model_group_id,
               case regexp_replace(mg.model_type, '^.*\.', '')
                   when 'DummyRegressor' then
                       case mg.hyperparameters->>'strategy'
                           when 'quantile' then (mg.hyperparameters->>'quantile')::double precision
                           when 'median'   then 0.5
                       end
                   when 'GradientBoostingRegressor' then
                       case when mg.hyperparameters->>'loss' = 'quantile'
                            then coalesce((mg.hyperparameters->>'alpha')::double precision, 0.9)
                       end
                   when 'HistGradientBoostingRegressor' then
                       case when mg.hyperparameters->>'loss' = 'quantile'
                            then (mg.hyperparameters->>'quantile')::double precision
                       end
                   when 'QuantileRegressor' then
                       coalesce((mg.hyperparameters->>'quantile')::double precision, 0.5)
                   when 'LGBMRegressor' then
                       case when mg.hyperparameters->>'objective' = 'quantile'
                            then coalesce((mg.hyperparameters->>'alpha')::double precision, 0.9)
                       end
               end as quantile
          from triage.model_groups mg) q
 where q.model_group_id = m.model_group_id
   and q.quantile is not null;
"""

HIGHER_IS_BETTER_WAPE = r"""
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
    or ($1 not in ('rmse', 'mae', 'wape', 'brier', 'log_loss',
                   'false positives@', 'false negatives@', 'fpr@')
        and $1 not like 'pinball@%'
        and $1 not like 'ece@%'));
$$;
"""

REGRESSION_METRIC_WAPE = r"""
create or replace function triage.regression_metric(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric         text,          -- 'rmse' | 'mae' | 'r2' | 'wape' | 'pinball@<tau>'
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
    v_quantile  double precision;
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
    elsif p_metric = 'wape' then
        -- Weighted absolute percentage error: the absolute error over the absolute volume.
        select sum(abs(score - outcome)) / nullif(sum(abs(outcome)), 0)
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
        -- Lower is better. Only a model that forecasts τ is scored; any other model gets NULL.
        v_tau := split_part(p_metric, '@', 2)::double precision;
        if v_tau <= 0 or v_tau >= 1 then
            raise exception 'pinball quantile must be in (0,1), got %', p_metric;
        end if;
        select m.forecast_quantile into v_quantile
          from triage.models m where m.model_id = p_model_id;
        if v_quantile is null or abs(v_quantile - v_tau) >= 1e-9 then
            return r;
        end if;
        select avg(case when outcome >= score
                        then v_tau * (outcome - score)
                        else (1 - v_tau) * (score - outcome) end)
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);
    else
        raise exception 'unknown regression metric % (expected rmse|mae|r2|wape|pinball@<tau>)', p_metric;
    end if;

    return r;
end;
$$;
"""


def upgrade() -> None:
    op.execute(FORECAST_QUANTILE_DDL)
    op.execute(HIGHER_IS_BETTER_WAPE)
    op.execute(REGRESSION_METRIC_WAPE)


def downgrade() -> None:
    op.execute(_revision_ddl("0020_pinball_metric", "REGRESSION_METRIC_PINBALL"))
    op.execute(_revision_ddl("0023_probability_metrics", "HIGHER_IS_BETTER_REGISTERED"))
    op.execute("alter table triage.models drop column forecast_quantile")
