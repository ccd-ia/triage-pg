"""regression residuals: history segments, error summaries, error by segment, largest errors

Four functions computed on read over ``scored_labels`` (0026). Nothing is stored.

* ``prior_label_history(model, split, n_history, band_edges)`` — per scored
  ``(entity, as_of_date)``: the entity's target history on the Experiment's label-date grid,
  the grid the target-history lags use. The grid of a date is the ``n_history`` most recent
  label dates of the labels artifact and horizon the date is labeled with (``label_source``,
  0031) whose window has closed, ``g + w <= as_of_date`` (ADR-0030). From the entity's labels
  on that grid: ``n_observed``, ``mean_value``, ``zero_share``, ``adi`` and ``cv2``, the
  ``band`` of the mean, the Syntetos–Boylan class, and ``cold_start`` (no label on the grid).

  A grid date with no label for the entity is not a zero: the entity was outside the cohort
  that date. ADI is therefore observed periods over periods with a positive label. CV² is
  ``(stddev_samp / mean)²`` of the positive labels. The class follows meio's ``sbc_class``
  thresholds (ADI 1.32, CV² 0.49), with three classes for the series the rule cannot place:
  ``no history``, ``no demand`` (no positive label) and ``one demand period``.

  Bands are ``(e_{i-1}, e_i]`` over the sorted edges, ``<= e_0`` below and ``> e_n`` above.
  ``band_ix`` orders them; it is NULL, with band ``no history``, on a cold start.

* ``regression_residual_summary(model, split)`` — per date, over the labeled rows: n, Σy, MAE,
  RMSE, WAPE, bias (mean of ŷ − y), the share of the absolute error held by the 100, 1,000 and
  top 1% largest errors, the share of negative forecasts, the forecast structure (distinct
  scores, the most frequent score and its share, minimum, maximum), and ``coverage_point``,
  the share of rows with y <= ŷ.

* ``regression_residuals_by_segment(model, split, cut)`` — the errors per segment, per date
  and pooled over the dates (``as_of_date`` NULL). ``cut`` is ``band``, ``sbc``,
  ``cold_start`` or a ``subset_hash``. ``share_of_abs_error`` is the segment's share of the
  absolute error of its scope (one date, or all of them).

* ``largest_errors(model, split, n)`` — the ``n`` rows with the largest absolute error, with
  the entity's display label when ``entity_attributes`` has a ``name``, ``label``,
  ``display_name`` or ``title``.

Downgrade drops the four functions.

Revision ID: 0033_regression_residuals
Revises: 0032_average_precision_ties
"""

from __future__ import annotations

from alembic import op

revision = "0033_regression_residuals"
down_revision = "0032_average_precision_ties"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- The entity's target history before each scored date, on the label-date grid of the lags.
create function triage.prior_label_history(
    p_model      bigint,
    p_split_kind triage.split_kind default 'test',
    p_n_history  integer default 12,
    p_band_edges double precision[] default '{0,10,100,1000}'
)
returns table (
    entity_id  bigint,
    as_of_date date,
    n_grid     integer,
    n_observed integer,
    mean_value double precision,
    zero_share double precision,
    adi        double precision,
    cv2        double precision,
    band       text,
    band_ix    integer,
    sbc_class  text,
    cold_start boolean
)
language sql
stable
as $$
    with edges as (
        select array(select e from unnest(p_band_edges) e where e is not null order by e)
            as e
    ),
    src as (
        select * from triage.label_source(p_model, p_split_kind)
    ),
    grid as (
        -- the n most recent label dates whose window closed by the scored date (ADR-0030)
        select src.as_of_date, g.grid_date, src.label_timespan, src.label_hash
        from   src
        cross  join lateral (
            select distinct l.as_of_date as grid_date
            from   triage.labels l
            where  l.label_hash = src.label_hash
              and  l.label_timespan = src.label_timespan
              and  l.as_of_date + l.label_timespan <= src.as_of_date
            order  by l.as_of_date desc
            limit  p_n_history
        ) g
    ),
    grid_size as (
        select grid.as_of_date, count(*)::integer as n from grid group by grid.as_of_date
    ),
    scored as (
        select distinct s.entity_id, s.as_of_date
        from   triage.scored_labels(p_model, p_split_kind) s
    ),
    stats as (
        select s.entity_id, s.as_of_date,
               count(*)::integer                                 as n_obs,
               avg(l.outcome)                                    as mean_value,
               count(*) filter (where l.outcome = 0)             as n_zero,
               count(*) filter (where l.outcome > 0)             as n_pos,
               avg(l.outcome) filter (where l.outcome > 0)       as mean_pos,
               stddev_samp(l.outcome) filter (where l.outcome > 0) as sd_pos
        from   scored s
        join   grid g on g.as_of_date = s.as_of_date
        join   triage.labels l
               on  l.entity_id = s.entity_id
               and l.as_of_date = g.grid_date
               and l.label_hash = g.label_hash
               and l.label_timespan = g.label_timespan
               and l.outcome is not null
        group  by s.entity_id, s.as_of_date
    ),
    measured as (
        select s.entity_id, s.as_of_date,
               coalesce(gs.n, 0)                                  as n_grid,
               coalesce(st.n_obs, 0)                              as n_observed,
               st.mean_value,
               st.n_zero::double precision / nullif(st.n_obs, 0)  as zero_share,
               st.n_obs::double precision / nullif(st.n_pos, 0)   as adi,
               case when st.n_pos = 1 then 0::double precision
                    when st.n_pos > 1 then power(st.sd_pos / st.mean_pos, 2)
               end                                                as cv2,
               st.n_pos,
               case when st.mean_value is not null
                    then (select count(*)::integer from unnest(edges.e) x
                          where st.mean_value > x)
               end                                                as band_ix,
               edges.e
        from   scored s
        cross  join edges
        left   join grid_size gs on gs.as_of_date = s.as_of_date
        left   join stats st on st.entity_id = s.entity_id and st.as_of_date = s.as_of_date
    )
    select m.entity_id, m.as_of_date, m.n_grid, m.n_observed, m.mean_value, m.zero_share,
           m.adi, m.cv2,
           case when m.n_observed = 0      then 'no history'
                when cardinality(m.e) = 0  then 'all'
                when m.band_ix = 0         then '<= ' || m.e[1]
                when m.band_ix = cardinality(m.e) then '> ' || m.e[cardinality(m.e)]
                else '(' || m.e[m.band_ix] || ', ' || m.e[m.band_ix + 1] || ']'
           end,
           m.band_ix,
           case when m.n_observed = 0                 then 'no history'
                when coalesce(m.n_pos, 0) = 0         then 'no demand'
                when m.n_pos = 1                      then 'one demand period'
                when m.adi < 1.32 and m.cv2 < 0.49    then 'smooth'
                when m.adi < 1.32                     then 'erratic'
                when m.cv2 < 0.49                     then 'intermittent'
                else 'lumpy'
           end,
           m.n_observed = 0
    from   measured m;
$$;

-- Per date: errors, their concentration and the forecast's structure, over the labeled rows.
create function triage.regression_residual_summary(
    p_model      bigint,
    p_split_kind triage.split_kind default 'test'
)
returns table (
    as_of_date             date,
    n                      bigint,
    sum_outcome            double precision,
    mae                    double precision,
    rmse                   double precision,
    wape                   double precision,
    bias                   double precision,
    top100_error_share     double precision,
    top1000_error_share    double precision,
    top1pct_error_share    double precision,
    negative_share         double precision,
    n_distinct_scores      bigint,
    mode_score             double precision,
    mode_share             double precision,
    min_score              double precision,
    max_score              double precision,
    coverage_point         double precision
)
language sql
stable
as $$
    with r as (
        select s.as_of_date, s.score, s.outcome, abs(s.score - s.outcome) as abs_err,
               row_number() over (partition by s.as_of_date
                                  order by abs(s.score - s.outcome) desc, s.entity_id)
                   as err_rank,
               count(*) over (partition by s.as_of_date) as n_date
        from   triage.scored_labels(p_model, p_split_kind) s
        where  s.outcome is not null and s.score is not null
    ),
    modes as (
        select distinct on (c.as_of_date) c.as_of_date, c.score, c.n
        from  (select r.as_of_date, r.score, count(*) as n
               from r group by r.as_of_date, r.score) c
        order by c.as_of_date, c.n desc, c.score
    )
    select r.as_of_date,
           count(*),
           sum(r.outcome),
           avg(r.abs_err),
           sqrt(avg(power(r.abs_err, 2))),
           sum(r.abs_err) / nullif(sum(abs(r.outcome)), 0),
           avg(r.score - r.outcome),
           sum(r.abs_err) filter (where r.err_rank <= 100) / nullif(sum(r.abs_err), 0),
           sum(r.abs_err) filter (where r.err_rank <= 1000) / nullif(sum(r.abs_err), 0),
           sum(r.abs_err) filter (where r.err_rank <= ceil(0.01 * r.n_date))
               / nullif(sum(r.abs_err), 0),
           avg((r.score < 0)::integer::double precision),
           count(distinct r.score),
           min(mo.score),
           min(mo.n)::double precision / count(*),
           min(r.score),
           max(r.score),
           avg((r.outcome <= r.score)::integer::double precision)
    from   r
    join   modes mo on mo.as_of_date = r.as_of_date
    group  by r.as_of_date
    order  by r.as_of_date;
$$;

-- Errors per segment of a cut, per date and pooled (as_of_date NULL).
create function triage.regression_residuals_by_segment(
    p_model      bigint,
    p_split_kind triage.split_kind default 'test',
    p_cut        text default 'band'
)
returns table (
    as_of_date         date,
    segment            text,
    segment_order      integer,
    n                  bigint,
    entities           bigint,
    sum_outcome        double precision,
    sum_forecast       double precision,
    mae                double precision,
    wape               double precision,
    bias               double precision,
    share_of_abs_error double precision
)
language plpgsql
stable
as $$
#variable_conflict use_column
declare
    v_subset text;
begin
    if p_cut not in ('band', 'sbc', 'cold_start') then
        select s.subset_hash into v_subset from triage.subsets s where s.subset_hash = p_cut;
        if v_subset is null then
            raise exception 'cut % is not band, sbc, cold_start or a subset_hash — list the'
                ' subsets with: select subset_hash, config->>''name'' from triage.subsets',
                p_cut;
        end if;
    end if;

    return query
    with r as (
        select s.entity_id, s.as_of_date, s.score, s.outcome,
               abs(s.score - s.outcome) as abs_err
        from   triage.scored_labels(p_model, p_split_kind) s
        where  s.outcome is not null and s.score is not null
    ),
    seg as (
        select r.*,
               case p_cut
                   when 'band' then h.band
                   when 'sbc' then h.sbc_class
                   when 'cold_start' then case when h.cold_start then 'cold start'
                                               else 'with history' end
                   else case when sm.entity_id is not null then 'in subset'
                             else 'outside subset' end
               end as segment,
               case p_cut
                   when 'band' then coalesce(h.band_ix, -1)
                   when 'sbc' then array_position(
                       array['smooth', 'erratic', 'intermittent', 'lumpy',
                             'one demand period', 'no demand', 'no history'],
                       h.sbc_class)
                   when 'cold_start' then h.cold_start::integer
                   else (sm.entity_id is null)::integer
               end as segment_order
        from   r
        left   join triage.prior_label_history(p_model, p_split_kind) h
               on p_cut in ('band', 'sbc', 'cold_start')
              and h.entity_id = r.entity_id and h.as_of_date = r.as_of_date
        left   join triage.subset_members sm
               on sm.subset_hash = v_subset
              and sm.entity_id = r.entity_id and sm.as_of_date = r.as_of_date
    ),
    g as (
        select seg.as_of_date, seg.segment, min(seg.segment_order) as segment_order,
               grouping(seg.as_of_date) as pooled,
               count(*) as n, count(distinct seg.entity_id) as entities,
               sum(seg.outcome) as sum_outcome, sum(seg.score) as sum_forecast,
               avg(seg.abs_err) as mae,
               sum(seg.abs_err) / nullif(sum(abs(seg.outcome)), 0) as wape,
               avg(seg.score - seg.outcome) as bias,
               sum(seg.abs_err) as abs_err
        from   seg
        group  by grouping sets ((seg.segment), (seg.as_of_date, seg.segment))
    )
    select g.as_of_date, g.segment, g.segment_order, g.n, g.entities, g.sum_outcome,
           g.sum_forecast, g.mae, g.wape, g.bias,
           g.abs_err / nullif(sum(g.abs_err) over (partition by g.pooled, g.as_of_date), 0)
    from   g
    order  by g.as_of_date nulls first, g.segment_order, g.segment;
end;
$$;

-- The rows with the largest absolute error, with the entity's display label if it has one.
create function triage.largest_errors(
    p_model      bigint,
    p_split_kind triage.split_kind default 'test',
    p_n          integer default 20
)
returns table (
    entity_id  bigint,
    as_of_date date,
    forecast   double precision,
    outcome    double precision,
    error      double precision,
    abs_error  double precision,
    label      text
)
language sql
stable
as $$
    with top as (
        select s.entity_id, s.as_of_date, s.score, s.outcome
        from   triage.scored_labels(p_model, p_split_kind) s
        where  s.outcome is not null and s.score is not null
        order  by abs(s.score - s.outcome) desc, s.as_of_date, s.entity_id
        limit  p_n
    )
    select top.entity_id, top.as_of_date, top.score, top.outcome,
           top.score - top.outcome, abs(top.score - top.outcome),
           (select coalesce(a->>'name', a->>'label', a->>'display_name', a->>'title')
              from (select triage.entity_attributes(top.entity_id) as a) attrs)
    from   top
    order  by abs(top.score - top.outcome) desc, top.as_of_date, top.entity_id;
$$;
"""


def upgrade() -> None:
    op.execute(UPGRADE_DDL)


def downgrade() -> None:
    op.execute(
        "drop function triage.largest_errors(bigint, triage.split_kind, integer);"
        " drop function triage.regression_residuals_by_segment("
        "bigint, triage.split_kind, text);"
        " drop function triage.regression_residual_summary(bigint, triage.split_kind);"
        " drop function triage.prior_label_history("
        "bigint, triage.split_kind, integer, double precision[]);"
    )
