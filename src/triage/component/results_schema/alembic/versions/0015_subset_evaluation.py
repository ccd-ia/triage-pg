"""subset-filtered evaluations — subset_hash becomes real (schema-design §8.6, plan P3)

Revision ID: 0015_subset_evaluation
Revises: 0014_bias_completeness
Create Date: 2026-07-06

Since 0001 the ``subset_hash`` slot existed everywhere (evaluations PK, function
signatures) but was only ever *recorded* — every metric ran over the full labeled
cohort. This migration implements the filtering, DSSG-compatibly: a subset is a named
cohort slice (``triage.subset_members``), and metrics treat the subset as the
population — ranks are recomputed WITHIN the subset (``row_number()`` runs after the
membership filter), so ``precision@100_abs`` on "district 7" means the top-100 of
district 7's own ranking. ``num_labeled``/``num_positive`` come from the subset.

Mechanics: ``labeled_ranks``/``survival_ranks`` gain ``p_subset_hash text default ''``
(empty = the exact 0011 behavior — the membership predicate is constant-true), every
metric function gains the same defaulted parameter and passes it through, and
``evaluate_model`` finally both FILTERS and stamps. Adding a defaulted parameter is a
new overload in PostgreSQL, so the old signatures are dropped first — callers with the
old arity (``compute_bias_metrics``, ``monitoring_calibration``) bind the new functions
via the default, unchanged.

Full-cohort guard: ``triage.leaderboard`` is recreated with ``subset_hash = ''`` so
subset rows never pollute the ranking (audition got its guard in 0013;
``evaluations_windowed`` already groups by subset_hash). The matview is refreshed here
so live dashboards don't hit an unpopulated view after migrating.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_subset_evaluation"
down_revision = "0014_bias_completeness"
branch_labels = None
depends_on = None


SUBSET_TABLE_DDL = r"""
create table triage.subset_members (
    subset_hash text   not null references triage.subsets(subset_hash) on delete cascade,
    entity_id   bigint not null,
    as_of_date  date   not null,
    primary key (subset_hash, entity_id, as_of_date)
);
create index subset_members_entity_date_idx
    on triage.subset_members (entity_id, as_of_date);
"""


# The 0011 pinned bodies + the membership predicate. The predicate sits in WHERE, so
# row_number() (which runs after WHERE) re-ranks within the subset automatically.
RANKS_DDL = r"""
drop function if exists triage.labeled_ranks(bigint, triage.split_kind, date, interval);
create or replace function triage.labeled_ranks(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_subset_hash    text default ''
)
returns table (
    entity_id  bigint,
    score      double precision,
    rank_abs   bigint,
    outcome    double precision
)
language plpgsql
stable
as $$
declare
    v_pin  text;
    v_pins integer;
begin
    select count(distinct la.artifact_id), min(la.artifact_id)
      into v_pins, v_pin
      from (select distinct pr.matrix_uuid
              from triage.predictions pr
             where pr.model_id   = p_model_id
               and pr.split_kind = p_split_kind
               and pr.as_of_date = p_as_of_date
               and pr.matrix_uuid is not null) pm
      join triage.matrices        mx using (matrix_uuid)
      join triage.artifact_inputs ai on ai.artifact_id = mx.artifact_id
      join triage.artifacts       la on la.artifact_id = ai.parent_id
                                    and la.kind = 'labels';
    if v_pins > 1 then
        raise exception 'ambiguous labels lineage for model % at % (% labels artifacts) —'
            ' evaluation refuses to mix label definitions', p_model_id, p_as_of_date, v_pins;
    end if;

    return query
    with p as materialized (
        -- latest score per entity within this (model, split, date) — the latest_predictions
        -- semantics, read directly from predictions (the view hides matrix_uuid).
        select distinct on (lp.entity_id)
               lp.entity_id, lp.score, lp.as_of_date
        from   triage.predictions lp
        where  lp.model_id   = p_model_id
          and  lp.split_kind = p_split_kind
          and  lp.as_of_date = p_as_of_date
        order  by lp.entity_id, lp.scored_at desc
    )
    select p.entity_id,
           p.score,
           -- re-rank within the *labeled* (and, when given, subset) population so top-k
           -- is contiguous — the subset is the population (DSSG subset semantics).
           row_number() over (order by p.score desc, p.entity_id) as rank_abs,
           l.outcome
    from   p
    join   triage.labels l
           on  l.entity_id      = p.entity_id
           and l.as_of_date     = p.as_of_date
           and l.label_timespan = p_label_timespan
           and (v_pin is null or l.label_hash = v_pin)
    where  l.outcome is not null
      and  (p_subset_hash = '' or exists (
              select 1 from triage.subset_members sm
              where sm.subset_hash = p_subset_hash
                and sm.entity_id   = p.entity_id
                and sm.as_of_date  = p.as_of_date));
end;
$$;

drop function if exists triage.survival_ranks(bigint, triage.split_kind, date, interval);
create or replace function triage.survival_ranks(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_subset_hash    text default ''
)
returns table (
    entity_id      bigint,
    score          double precision,
    duration       double precision,
    event_observed boolean
)
language plpgsql
stable
as $$
declare
    v_pin  text;
    v_pins integer;
begin
    select count(distinct la.artifact_id), min(la.artifact_id)
      into v_pins, v_pin
      from (select distinct pr.matrix_uuid
              from triage.predictions pr
             where pr.model_id   = p_model_id
               and pr.split_kind = p_split_kind
               and pr.as_of_date = p_as_of_date
               and pr.matrix_uuid is not null) pm
      join triage.matrices        mx using (matrix_uuid)
      join triage.artifact_inputs ai on ai.artifact_id = mx.artifact_id
      join triage.artifacts       la on la.artifact_id = ai.parent_id
                                    and la.kind = 'labels';
    if v_pins > 1 then
        raise exception 'ambiguous labels lineage for model % at % (% labels artifacts) —'
            ' evaluation refuses to mix label definitions', p_model_id, p_as_of_date, v_pins;
    end if;

    return query
    with p as materialized (
        select distinct on (lp.entity_id)
               lp.entity_id, lp.score, lp.as_of_date
        from   triage.predictions lp
        where  lp.model_id   = p_model_id
          and  lp.split_kind = p_split_kind
          and  lp.as_of_date = p_as_of_date
        order  by lp.entity_id, lp.scored_at desc
    )
    select p.entity_id,
           p.score,
           l.duration,
           l.event_observed
    from   p
    join   triage.labels l
           on  l.entity_id      = p.entity_id
           and l.as_of_date     = p.as_of_date
           and l.label_timespan = p_label_timespan
           and (v_pin is null or l.label_hash = v_pin)
    where  l.duration is not null
      and  l.event_observed is not null
      and  (p_subset_hash = '' or exists (
              select 1 from triage.subset_members sm
              where sm.subset_hash = p_subset_hash
                and sm.entity_id   = p.entity_id
                and sm.as_of_date  = p.as_of_date));
end;
$$;
"""


# Every metric function: the 0002/0011 bodies verbatim, with p_subset_hash appended to
# the signature (defaulted) and to every labeled_ranks/survival_ranks call.
METRICS_DDL = r"""
drop function if exists triage.precision_at_k(bigint, triage.split_kind, date, interval, text);
create or replace function triage.precision_at_k(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_parameter      text,
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
    k           integer;
    tp          integer;          -- realized true positives in the deterministic top-k
    base_rate   double precision; -- K/N
    worst_tp    integer;
    best_tp     integer;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_labeled = 0 then
        return r;  -- everything null but the counts
    end if;

    k := triage.resolve_k(p_parameter, n_labeled);
    if k = 0 then
        r.value := null;
        return r;
    end if;

    -- Deterministic realized TP among the top-k by the view's tiebreak.
    select coalesce(sum((outcome > 0)::int), 0)::int
      into tp
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash)
     where rank_abs <= k;

    r.value := tp::double precision / k;

    -- Analytic random-ranking baseline (hypergeometric: K positives of N, k drawn).
    base_rate      := n_positive::double precision / n_labeled;
    r.value_expected := base_rate;            -- E[precision@k] under random ranking == base rate
    if n_labeled > 1 then
        r.value_std := sqrt(k * base_rate * (1 - base_rate) * ((n_labeled - k)::double precision
                            / (n_labeled - 1))) / k;
    else
        r.value_std := 0;
    end if;

    -- Deterministic worst/best-case tie bounds: how few / many positives could
    -- sit in the top-k if the boundary tie group were ordered adversarially /
    -- favourably. Computed analytically over the score-tie block straddling k.
    -- Rows strictly above the boundary block are fixed; rows within it are free.
    with ranked as (
        select score, outcome
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash)
    ),
    boundary as (
        select (select score from ranked order by score desc offset (k - 1) limit 1) as cutoff_score
    ),
    -- fixed = rows with score strictly greater than the cutoff score (always selected)
    fixed as (
        select count(*) filter (where outcome > 0)::int as fixed_pos,
               count(*)::int                            as fixed_n
        from   ranked, boundary
        where  ranked.score > boundary.cutoff_score
    ),
    -- tie block = rows whose score == cutoff (the ambiguous boundary group)
    tie as (
        select count(*) filter (where outcome > 0)::int as tie_pos,
               count(*)::int                            as tie_n
        from   ranked, boundary
        where  ranked.score = boundary.cutoff_score
    )
    select
        fixed.fixed_pos + greatest(0, (k - fixed.fixed_n) - (tie.tie_n - tie.tie_pos)),
        fixed.fixed_pos + least(tie.tie_pos, k - fixed.fixed_n)
      into worst_tp, best_tp
      from fixed, tie;

    r.value_worst := worst_tp::double precision / k;
    r.value_best  := best_tp::double precision / k;

    return r;
end;
$$;

drop function if exists triage.recall_at_k(bigint, triage.split_kind, date, interval, text);
create or replace function triage.recall_at_k(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_parameter      text,
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
    k           integer;
    tp          integer;
    worst_tp    integer;
    best_tp     integer;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_labeled = 0 or n_positive = 0 then
        return r;  -- recall undefined with no positives
    end if;

    k := triage.resolve_k(p_parameter, n_labeled);
    if k = 0 then
        r.value := 0;
        r.value_expected := 0;
        r.value_std := 0;
        return r;
    end if;

    select coalesce(sum((outcome > 0)::int), 0)::int
      into tp
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash)
     where rank_abs <= k;

    r.value := tp::double precision / n_positive;

    -- Random ranking: E[TP] = k*P/N, so E[recall] = k/N; std scales by 1/P.
    r.value_expected := k::double precision / n_labeled;
    if n_labeled > 1 then
        r.value_std := sqrt(k * (n_positive::double precision / n_labeled)
                            * (1 - n_positive::double precision / n_labeled)
                            * ((n_labeled - k)::double precision / (n_labeled - 1)))
                       / n_positive;
    else
        r.value_std := 0;
    end if;

    with ranked as (
        select score, outcome
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash)
    ),
    boundary as (
        select (select score from ranked order by score desc offset (k - 1) limit 1) as cutoff_score
    ),
    fixed as (
        select count(*) filter (where outcome > 0)::int as fixed_pos,
               count(*)::int                            as fixed_n
        from   ranked, boundary
        where  ranked.score > boundary.cutoff_score
    ),
    tie as (
        select count(*) filter (where outcome > 0)::int as tie_pos,
               count(*)::int                            as tie_n
        from   ranked, boundary
        where  ranked.score = boundary.cutoff_score
    )
    select
        fixed.fixed_pos + greatest(0, (k - fixed.fixed_n) - (tie.tie_n - tie.tie_pos)),
        fixed.fixed_pos + least(tie.tie_pos, k - fixed.fixed_n)
      into worst_tp, best_tp
      from fixed, tie;

    r.value_worst := worst_tp::double precision / n_positive;
    r.value_best  := best_tp::double precision / n_positive;

    return r;
end;
$$;

drop function if exists triage.auc_roc(bigint, triage.split_kind, date, interval);
create or replace function triage.auc_roc(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
    n_negative  integer;
    sum_ranks   double precision;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;
    n_negative     := n_labeled - n_positive;

    if n_positive = 0 or n_negative = 0 then
        return r;  -- AUC undefined when one class is empty
    end if;

    -- mid-rank ascending by score; average rank within score ties.
    with ranked as (
        select outcome,
               avg(rk) over (partition by score) as mid_rank
        from (
            select score, outcome,
                   row_number() over (order by score asc) as rk
            from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash)
        ) s
    )
    select sum(mid_rank) filter (where outcome > 0)
      into sum_ranks
      from ranked;

    r.value := (sum_ranks - n_positive::double precision * (n_positive + 1) / 2)
               / (n_positive::double precision * n_negative);
    return r;
end;
$$;

drop function if exists triage.average_precision(bigint, triage.split_kind, date, interval);
create or replace function triage.average_precision(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
    ap          double precision;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_positive = 0 then
        return r;
    end if;

    with ordered as (
        select outcome,
               row_number() over (order by score desc, entity_id) as rnk
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash)
    ),
    cum as (
        select rnk, outcome,
               sum((outcome > 0)::int) over (order by rnk
                    rows between unbounded preceding and current row) as cum_tp
        from   ordered
    )
    select coalesce(sum((cum_tp::double precision / rnk)) filter (where outcome > 0), 0) / n_positive
      into ap
      from cum;

    r.value := ap;
    return r;
end;
$$;

drop function if exists triage.regression_metric(bigint, triage.split_kind, date, interval, text);
create or replace function triage.regression_metric(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric         text,          -- 'rmse' | 'mae' | 'r2'
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
            r.value := null;  -- R² undefined when the target is constant
        else
            r.value := 1 - ss_res / ss_tot;
        end if;
    else
        raise exception 'unknown regression metric % (expected rmse|mae|r2)', p_metric;
    end if;

    return r;
end;
$$;

drop function if exists triage.c_index(bigint, triage.split_kind, date, interval);
create or replace function triage.c_index(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_subset_hash    text default ''
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r            triage.metric_result;
    n_labeled    integer;
    n_events     integer;
    v_concordant double precision;
    v_tied       double precision;
    v_comparable double precision;
begin
    select count(*)::int, coalesce(sum(event_observed::int), 0)::int
      into n_labeled, n_events
      from triage.survival_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash);

    r.num_labeled  := n_labeled;
    r.num_positive := n_events;

    if n_labeled = 0 or n_events = 0 then
        return r;  -- C undefined with no comparable pairs possible
    end if;

    -- MATERIALIZED is load-bearing: self-joining the set-returning function directly lets
    -- the planner inline it and re-execute the whole underlying scan per outer row.
    -- Risk-score ties use sksurv's tolerance (concordance_index_censored tied_tol=1e-8):
    -- |Δscore| <= 1e-8 is a tie (0.5), concordant needs Δscore > 1e-8 — exact-equality
    -- ties diverge from the reference on continuous scores (observed live at ~1e-6).
    with s as materialized (
        select score, duration, event_observed
        from triage.survival_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan, p_subset_hash)
    )
    select count(*) filter (where a.score - b.score > 1e-8),
           count(*) filter (where abs(a.score - b.score) <= 1e-8),
           count(*)
      into v_concordant, v_tied, v_comparable
      from s a
      join s b
        on (a.duration < b.duration and a.event_observed)
        or (a.duration = b.duration and a.event_observed and not b.event_observed);

    if v_comparable = 0 then
        return r;
    end if;

    r.value          := (v_concordant + 0.5 * v_tied) / v_comparable;
    r.value_worst    := 0;
    r.value_best     := 1;
    r.value_expected := 0.5;
    return r;
end;
$$;
"""


# evaluate_model: signature unchanged (p_subset_hash existed since 0002) — the body now
# PASSES it into every metric call instead of only stamping it on the rows.
EVALUATE_MODEL_DDL = r"""
create or replace function triage.evaluate_model(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric_config  jsonb,
    p_subset_hash    text default ''
)
returns integer  -- number of evaluation rows written
language plpgsql
volatile
as $$
declare
    v_metric    text;
    v_threshold text;
    res         triage.metric_result;
    written     integer := 0;
begin
    -- threshold metrics (precision@ / recall@) × thresholds
    for v_metric in
        select jsonb_array_elements_text(coalesce(p_metric_config->'metrics', '[]'::jsonb))
    loop
        if v_metric in ('precision@', 'recall@') then
            for v_threshold in
                select jsonb_array_elements_text(coalesce(p_metric_config->'thresholds', '[]'::jsonb))
            loop
                if v_metric = 'precision@' then
                    res := triage.precision_at_k(p_model_id, p_split_kind, p_as_of_date,
                                                 p_label_timespan, v_threshold, p_subset_hash);
                else
                    res := triage.recall_at_k(p_model_id, p_split_kind, p_as_of_date,
                                              p_label_timespan, v_threshold, p_subset_hash);
                end if;
                insert into triage.evaluations (
                    model_id, split_kind, as_of_date, subset_hash, metric, parameter,
                    value, value_worst, value_best, value_expected, value_std,
                    num_labeled, num_positive, computed_at)
                values (
                    p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, v_threshold,
                    res.value, res.value_worst, res.value_best, res.value_expected, res.value_std,
                    res.num_labeled, res.num_positive, now())
                on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
                do update set value = excluded.value,
                              value_worst = excluded.value_worst,
                              value_best = excluded.value_best,
                              value_expected = excluded.value_expected,
                              value_std = excluded.value_std,
                              num_labeled = excluded.num_labeled,
                              num_positive = excluded.num_positive,
                              computed_at = excluded.computed_at;
                written := written + 1;
            end loop;
        elsif v_metric in ('auc_roc', 'average_precision') then
            if v_metric = 'auc_roc' then
                res := triage.auc_roc(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                      p_subset_hash);
            else
                res := triage.average_precision(p_model_id, p_split_kind, p_as_of_date,
                                                p_label_timespan, p_subset_hash);
            end if;
            insert into triage.evaluations (
                model_id, split_kind, as_of_date, subset_hash, metric, parameter,
                value, value_worst, value_best, value_expected, value_std,
                num_labeled, num_positive, computed_at)
            values (
                p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, '',
                res.value, res.value_worst, res.value_best, res.value_expected, res.value_std,
                res.num_labeled, res.num_positive, now())
            on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
            do update set value = excluded.value,
                          value_worst = excluded.value_worst,
                          value_best = excluded.value_best,
                          value_expected = excluded.value_expected,
                          value_std = excluded.value_std,
                          num_labeled = excluded.num_labeled,
                          num_positive = excluded.num_positive,
                          computed_at = excluded.computed_at;
            written := written + 1;
        else
            raise exception 'unknown classification metric % (expected precision@|recall@|auc_roc|average_precision)', v_metric;
        end if;
    end loop;

    -- regression metrics (scalar; threshold columns null, num_positive null)
    for v_metric in
        select jsonb_array_elements_text(coalesce(p_metric_config->'regression_metrics', '[]'::jsonb))
    loop
        res := triage.regression_metric(p_model_id, p_split_kind, p_as_of_date,
                                        p_label_timespan, v_metric, p_subset_hash);
        insert into triage.evaluations (
            model_id, split_kind, as_of_date, subset_hash, metric, parameter,
            value, value_worst, value_best, value_expected, value_std,
            num_labeled, num_positive, computed_at)
        values (
            p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, '',
            res.value, null, null, null, null,
            res.num_labeled, null, now())
        on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
        do update set value = excluded.value,
                      value_worst = null, value_best = null,
                      value_expected = null, value_std = null,
                      num_labeled = excluded.num_labeled,
                      num_positive = null,
                      computed_at = excluded.computed_at;
        written := written + 1;
    end loop;

    -- survival metrics (C-index; the ADR-0010 ranking spine, landed by 0011)
    for v_metric in
        select jsonb_array_elements_text(coalesce(p_metric_config->'survival_metrics', '[]'::jsonb))
    loop
        if v_metric = 'c_index' then
            res := triage.c_index(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                  p_subset_hash);
        else
            raise exception 'unknown survival metric % (expected c_index)', v_metric;
        end if;
        insert into triage.evaluations (
            model_id, split_kind, as_of_date, subset_hash, metric, parameter,
            value, value_worst, value_best, value_expected, value_std,
            num_labeled, num_positive, computed_at)
        values (
            p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, '',
            res.value, res.value_worst, res.value_best, res.value_expected, null,
            res.num_labeled, res.num_positive, now())
        on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
        do update set value = excluded.value,
                      value_worst = excluded.value_worst,
                      value_best = excluded.value_best,
                      value_expected = excluded.value_expected,
                      value_std = null,
                      num_labeled = excluded.num_labeled,
                      num_positive = excluded.num_positive,
                      computed_at = excluded.computed_at;
        written := written + 1;
    end loop;

    return written;
end;
$$;
"""


# Full-cohort guard on the leaderboard (0005 definition + subset_hash = '').
LEADERBOARD_DDL = r"""
drop materialized view if exists triage.leaderboard;
create materialized view triage.leaderboard as
select r.experiment_hash, m.run_id, mg.model_group_id, mg.model_type, e.split_kind,
       e.metric, e.parameter, e.as_of_date,
       e.value, e.value_expected, e.value_std, m.model_id, m.train_end_time
from   triage.evaluations e
join   triage.models       m  on m.model_id = e.model_id
join   triage.model_groups mg on mg.model_group_id = m.model_group_id
join   triage.runs         r  on r.run_id = m.run_id
where  e.subset_hash = ''
with no data;
refresh materialized view triage.leaderboard;
"""


def upgrade():
    op.execute(SUBSET_TABLE_DDL)
    op.execute(RANKS_DDL)
    op.execute(METRICS_DDL)
    op.execute(EVALUATE_MODEL_DDL)
    op.execute(LEADERBOARD_DDL)


# ------------------------------------------------------------------- downgrade
# The pre-0015 definitions, verbatim (the same embed-the-prior-bodies pattern 0011
# uses for its own downgrade): ranks + c_index + evaluate_model restored to their
# 0011 shapes, the classification/regression metrics to their 0002 shapes, the
# leaderboard to its 0005 shape.
DOWNGRADE_DROP_DDL = r"""
drop materialized view if exists triage.leaderboard;

drop function if exists triage.evaluate_model(bigint, triage.split_kind, date, interval, jsonb, text);
drop function if exists triage.c_index(bigint, triage.split_kind, date, interval, text);
drop function if exists triage.regression_metric(bigint, triage.split_kind, date, interval, text, text);
drop function if exists triage.average_precision(bigint, triage.split_kind, date, interval, text);
drop function if exists triage.auc_roc(bigint, triage.split_kind, date, interval, text);
drop function if exists triage.recall_at_k(bigint, triage.split_kind, date, interval, text, text);
drop function if exists triage.precision_at_k(bigint, triage.split_kind, date, interval, text, text);
drop function if exists triage.survival_ranks(bigint, triage.split_kind, date, interval, text);
drop function if exists triage.labeled_ranks(bigint, triage.split_kind, date, interval, text);

drop table triage.subset_members;
"""

DOWNGRADE_RANKS_0011_DDL = r"""
create or replace function triage.labeled_ranks(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval
)
returns table (
    entity_id  bigint,
    score      double precision,
    rank_abs   bigint,
    outcome    double precision
)
language plpgsql
stable
as $$
declare
    v_pin  text;
    v_pins integer;
begin
    select count(distinct la.artifact_id), min(la.artifact_id)
      into v_pins, v_pin
      from (select distinct pr.matrix_uuid
              from triage.predictions pr
             where pr.model_id   = p_model_id
               and pr.split_kind = p_split_kind
               and pr.as_of_date = p_as_of_date
               and pr.matrix_uuid is not null) pm
      join triage.matrices        mx using (matrix_uuid)
      join triage.artifact_inputs ai on ai.artifact_id = mx.artifact_id
      join triage.artifacts       la on la.artifact_id = ai.parent_id
                                    and la.kind = 'labels';
    if v_pins > 1 then
        raise exception 'ambiguous labels lineage for model % at % (% labels artifacts) —'
            ' evaluation refuses to mix label definitions', p_model_id, p_as_of_date, v_pins;
    end if;

    return query
    with p as materialized (
        select distinct on (lp.entity_id)
               lp.entity_id, lp.score, lp.as_of_date
        from   triage.predictions lp
        where  lp.model_id   = p_model_id
          and  lp.split_kind = p_split_kind
          and  lp.as_of_date = p_as_of_date
        order  by lp.entity_id, lp.scored_at desc
    )
    select p.entity_id,
           p.score,
           row_number() over (order by p.score desc, p.entity_id) as rank_abs,
           l.outcome
    from   p
    join   triage.labels l
           on  l.entity_id      = p.entity_id
           and l.as_of_date     = p.as_of_date
           and l.label_timespan = p_label_timespan
           and (v_pin is null or l.label_hash = v_pin)
    where  l.outcome is not null;
end;
$$;

create or replace function triage.survival_ranks(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval
)
returns table (
    entity_id      bigint,
    score          double precision,
    duration       double precision,
    event_observed boolean
)
language plpgsql
stable
as $$
declare
    v_pin  text;
    v_pins integer;
begin
    select count(distinct la.artifact_id), min(la.artifact_id)
      into v_pins, v_pin
      from (select distinct pr.matrix_uuid
              from triage.predictions pr
             where pr.model_id   = p_model_id
               and pr.split_kind = p_split_kind
               and pr.as_of_date = p_as_of_date
               and pr.matrix_uuid is not null) pm
      join triage.matrices        mx using (matrix_uuid)
      join triage.artifact_inputs ai on ai.artifact_id = mx.artifact_id
      join triage.artifacts       la on la.artifact_id = ai.parent_id
                                    and la.kind = 'labels';
    if v_pins > 1 then
        raise exception 'ambiguous labels lineage for model % at % (% labels artifacts) —'
            ' evaluation refuses to mix label definitions', p_model_id, p_as_of_date, v_pins;
    end if;

    return query
    with p as materialized (
        select distinct on (lp.entity_id)
               lp.entity_id, lp.score, lp.as_of_date
        from   triage.predictions lp
        where  lp.model_id   = p_model_id
          and  lp.split_kind = p_split_kind
          and  lp.as_of_date = p_as_of_date
        order  by lp.entity_id, lp.scored_at desc
    )
    select p.entity_id,
           p.score,
           l.duration,
           l.event_observed
    from   p
    join   triage.labels l
           on  l.entity_id      = p.entity_id
           and l.as_of_date     = p.as_of_date
           and l.label_timespan = p_label_timespan
           and (v_pin is null or l.label_hash = v_pin)
    where  l.duration is not null
      and  l.event_observed is not null;
end;
$$;

create or replace function triage.c_index(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r            triage.metric_result;
    n_labeled    integer;
    n_events     integer;
    v_concordant double precision;
    v_tied       double precision;
    v_comparable double precision;
begin
    select count(*)::int, coalesce(sum(event_observed::int), 0)::int
      into n_labeled, n_events
      from triage.survival_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    r.num_labeled  := n_labeled;
    r.num_positive := n_events;

    if n_labeled = 0 or n_events = 0 then
        return r;
    end if;

    with s as materialized (
        select score, duration, event_observed
        from triage.survival_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
    )
    select count(*) filter (where a.score - b.score > 1e-8),
           count(*) filter (where abs(a.score - b.score) <= 1e-8),
           count(*)
      into v_concordant, v_tied, v_comparable
      from s a
      join s b
        on (a.duration < b.duration and a.event_observed)
        or (a.duration = b.duration and a.event_observed and not b.event_observed);

    if v_comparable = 0 then
        return r;
    end if;

    r.value          := (v_concordant + 0.5 * v_tied) / v_comparable;
    r.value_worst    := 0;
    r.value_best     := 1;
    r.value_expected := 0.5;
    return r;
end;
$$;
"""

DOWNGRADE_METRICS_0002_DDL = r"""
create or replace function triage.precision_at_k(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_parameter      text
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
    k           integer;
    tp          integer;
    base_rate   double precision;
    worst_tp    integer;
    best_tp     integer;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_labeled = 0 then
        return r;
    end if;

    k := triage.resolve_k(p_parameter, n_labeled);
    if k = 0 then
        r.value := null;
        return r;
    end if;

    select coalesce(sum((outcome > 0)::int), 0)::int
      into tp
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
     where rank_abs <= k;

    r.value := tp::double precision / k;

    base_rate      := n_positive::double precision / n_labeled;
    r.value_expected := base_rate;
    if n_labeled > 1 then
        r.value_std := sqrt(k * base_rate * (1 - base_rate) * ((n_labeled - k)::double precision
                            / (n_labeled - 1))) / k;
    else
        r.value_std := 0;
    end if;

    with ranked as (
        select score, outcome
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
    ),
    boundary as (
        select (select score from ranked order by score desc offset (k - 1) limit 1) as cutoff_score
    ),
    fixed as (
        select count(*) filter (where outcome > 0)::int as fixed_pos,
               count(*)::int                            as fixed_n
        from   ranked, boundary
        where  ranked.score > boundary.cutoff_score
    ),
    tie as (
        select count(*) filter (where outcome > 0)::int as tie_pos,
               count(*)::int                            as tie_n
        from   ranked, boundary
        where  ranked.score = boundary.cutoff_score
    )
    select
        fixed.fixed_pos + greatest(0, (k - fixed.fixed_n) - (tie.tie_n - tie.tie_pos)),
        fixed.fixed_pos + least(tie.tie_pos, k - fixed.fixed_n)
      into worst_tp, best_tp
      from fixed, tie;

    r.value_worst := worst_tp::double precision / k;
    r.value_best  := best_tp::double precision / k;

    return r;
end;
$$;

create or replace function triage.recall_at_k(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_parameter      text
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
    k           integer;
    tp          integer;
    worst_tp    integer;
    best_tp     integer;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_labeled = 0 or n_positive = 0 then
        return r;
    end if;

    k := triage.resolve_k(p_parameter, n_labeled);
    if k = 0 then
        r.value := 0;
        r.value_expected := 0;
        r.value_std := 0;
        return r;
    end if;

    select coalesce(sum((outcome > 0)::int), 0)::int
      into tp
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
     where rank_abs <= k;

    r.value := tp::double precision / n_positive;

    r.value_expected := k::double precision / n_labeled;
    if n_labeled > 1 then
        r.value_std := sqrt(k * (n_positive::double precision / n_labeled)
                            * (1 - n_positive::double precision / n_labeled)
                            * ((n_labeled - k)::double precision / (n_labeled - 1)))
                       / n_positive;
    else
        r.value_std := 0;
    end if;

    with ranked as (
        select score, outcome
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
    ),
    boundary as (
        select (select score from ranked order by score desc offset (k - 1) limit 1) as cutoff_score
    ),
    fixed as (
        select count(*) filter (where outcome > 0)::int as fixed_pos,
               count(*)::int                            as fixed_n
        from   ranked, boundary
        where  ranked.score > boundary.cutoff_score
    ),
    tie as (
        select count(*) filter (where outcome > 0)::int as tie_pos,
               count(*)::int                            as tie_n
        from   ranked, boundary
        where  ranked.score = boundary.cutoff_score
    )
    select
        fixed.fixed_pos + greatest(0, (k - fixed.fixed_n) - (tie.tie_n - tie.tie_pos)),
        fixed.fixed_pos + least(tie.tie_pos, k - fixed.fixed_n)
      into worst_tp, best_tp
      from fixed, tie;

    r.value_worst := worst_tp::double precision / n_positive;
    r.value_best  := best_tp::double precision / n_positive;

    return r;
end;
$$;

create or replace function triage.auc_roc(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
    n_negative  integer;
    sum_ranks   double precision;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;
    n_negative     := n_labeled - n_positive;

    if n_positive = 0 or n_negative = 0 then
        return r;
    end if;

    with ranked as (
        select outcome,
               avg(rk) over (partition by score) as mid_rank
        from (
            select score, outcome,
                   row_number() over (order by score asc) as rk
            from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
        ) s
    )
    select sum(mid_rank) filter (where outcome > 0)
      into sum_ranks
      from ranked;

    r.value := (sum_ranks - n_positive::double precision * (n_positive + 1) / 2)
               / (n_positive::double precision * n_negative);
    return r;
end;
$$;

create or replace function triage.average_precision(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval
)
returns triage.metric_result
language plpgsql
stable
as $$
declare
    r           triage.metric_result;
    n_labeled   integer;
    n_positive  integer;
    ap          double precision;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_positive = 0 then
        return r;
    end if;

    with ordered as (
        select outcome,
               row_number() over (order by score desc, entity_id) as rnk
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
    ),
    cum as (
        select rnk, outcome,
               sum((outcome > 0)::int) over (order by rnk
                    rows between unbounded preceding and current row) as cum_tp
        from   ordered
    )
    select coalesce(sum((cum_tp::double precision / rnk)) filter (where outcome > 0), 0) / n_positive
      into ap
      from cum;

    r.value := ap;
    return r;
end;
$$;

create or replace function triage.regression_metric(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric         text
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
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    r.num_labeled := n_labeled;
    if n_labeled = 0 then
        return r;
    end if;

    if p_metric = 'rmse' then
        select sqrt(avg(power(score - outcome, 2)))
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);
    elsif p_metric = 'mae' then
        select avg(abs(score - outcome))
          into r.value
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);
    elsif p_metric = 'r2' then
        select sum(power(outcome - score, 2)),
               sum(power(outcome - mean_y, 2))
          into ss_res, ss_tot
          from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);
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

DOWNGRADE_LEADERBOARD_0005_DDL = r"""
create materialized view triage.leaderboard as
select r.experiment_hash, m.run_id, mg.model_group_id, mg.model_type, e.split_kind,
       e.metric, e.parameter, e.as_of_date,
       e.value, e.value_expected, e.value_std, m.model_id, m.train_end_time
from   triage.evaluations e
join   triage.models       m  on m.model_id = e.model_id
join   triage.model_groups mg on mg.model_group_id = m.model_group_id
join   triage.runs         r  on r.run_id = m.run_id
with no data;
refresh materialized view triage.leaderboard;
"""


# evaluate_model restored to the 0011 body verbatim: same signature as 0015's (the
# subset parameter existed since 0002), stamping-only semantics.
DOWNGRADE_EVALUATE_MODEL_0011_DDL = r"""
create or replace function triage.evaluate_model(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric_config  jsonb,
    p_subset_hash    text default ''
)
returns integer  -- number of evaluation rows written
language plpgsql
volatile
as $$
declare
    v_metric    text;
    v_threshold text;
    res         triage.metric_result;
    written     integer := 0;
begin
    -- threshold metrics (precision@ / recall@) × thresholds
    for v_metric in
        select jsonb_array_elements_text(coalesce(p_metric_config->'metrics', '[]'::jsonb))
    loop
        if v_metric in ('precision@', 'recall@') then
            for v_threshold in
                select jsonb_array_elements_text(coalesce(p_metric_config->'thresholds', '[]'::jsonb))
            loop
                if v_metric = 'precision@' then
                    res := triage.precision_at_k(p_model_id, p_split_kind, p_as_of_date,
                                                 p_label_timespan, v_threshold);
                else
                    res := triage.recall_at_k(p_model_id, p_split_kind, p_as_of_date,
                                              p_label_timespan, v_threshold);
                end if;
                insert into triage.evaluations (
                    model_id, split_kind, as_of_date, subset_hash, metric, parameter,
                    value, value_worst, value_best, value_expected, value_std,
                    num_labeled, num_positive, computed_at)
                values (
                    p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, v_threshold,
                    res.value, res.value_worst, res.value_best, res.value_expected, res.value_std,
                    res.num_labeled, res.num_positive, now())
                on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
                do update set value = excluded.value,
                              value_worst = excluded.value_worst,
                              value_best = excluded.value_best,
                              value_expected = excluded.value_expected,
                              value_std = excluded.value_std,
                              num_labeled = excluded.num_labeled,
                              num_positive = excluded.num_positive,
                              computed_at = excluded.computed_at;
                written := written + 1;
            end loop;
        elsif v_metric in ('auc_roc', 'average_precision') then
            if v_metric = 'auc_roc' then
                res := triage.auc_roc(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);
            else
                res := triage.average_precision(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);
            end if;
            insert into triage.evaluations (
                model_id, split_kind, as_of_date, subset_hash, metric, parameter,
                value, value_worst, value_best, value_expected, value_std,
                num_labeled, num_positive, computed_at)
            values (
                p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, '',
                res.value, res.value_worst, res.value_best, res.value_expected, res.value_std,
                res.num_labeled, res.num_positive, now())
            on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
            do update set value = excluded.value,
                          value_worst = excluded.value_worst,
                          value_best = excluded.value_best,
                          value_expected = excluded.value_expected,
                          value_std = excluded.value_std,
                          num_labeled = excluded.num_labeled,
                          num_positive = excluded.num_positive,
                          computed_at = excluded.computed_at;
            written := written + 1;
        else
            raise exception 'unknown classification metric % (expected precision@|recall@|auc_roc|average_precision)', v_metric;
        end if;
    end loop;

    -- regression metrics (scalar; threshold columns null, num_positive null)
    for v_metric in
        select jsonb_array_elements_text(coalesce(p_metric_config->'regression_metrics', '[]'::jsonb))
    loop
        res := triage.regression_metric(p_model_id, p_split_kind, p_as_of_date,
                                        p_label_timespan, v_metric);
        insert into triage.evaluations (
            model_id, split_kind, as_of_date, subset_hash, metric, parameter,
            value, value_worst, value_best, value_expected, value_std,
            num_labeled, num_positive, computed_at)
        values (
            p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, '',
            res.value, null, null, null, null,
            res.num_labeled, null, now())
        on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
        do update set value = excluded.value,
                      value_worst = null, value_best = null,
                      value_expected = null, value_std = null,
                      num_labeled = excluded.num_labeled,
                      num_positive = null,
                      computed_at = excluded.computed_at;
        written := written + 1;
    end loop;

    -- survival metrics (C-index; the ADR-0010 ranking spine, landed by 0011)
    for v_metric in
        select jsonb_array_elements_text(coalesce(p_metric_config->'survival_metrics', '[]'::jsonb))
    loop
        if v_metric = 'c_index' then
            res := triage.c_index(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);
        else
            raise exception 'unknown survival metric % (expected c_index)', v_metric;
        end if;
        insert into triage.evaluations (
            model_id, split_kind, as_of_date, subset_hash, metric, parameter,
            value, value_worst, value_best, value_expected, value_std,
            num_labeled, num_positive, computed_at)
        values (
            p_model_id, p_split_kind, p_as_of_date, p_subset_hash, v_metric, '',
            res.value, res.value_worst, res.value_best, res.value_expected, null,
            res.num_labeled, res.num_positive, now())
        on conflict (model_id, split_kind, as_of_date, subset_hash, metric, parameter)
        do update set value = excluded.value,
                      value_worst = excluded.value_worst,
                      value_best = excluded.value_best,
                      value_expected = excluded.value_expected,
                      value_std = null,
                      num_labeled = excluded.num_labeled,
                      num_positive = excluded.num_positive,
                      computed_at = excluded.computed_at;
        written := written + 1;
    end loop;

    return written;
end;
$$;
"""


def downgrade():
    op.execute(DOWNGRADE_DROP_DDL)
    op.execute(DOWNGRADE_RANKS_0011_DDL)
    op.execute(DOWNGRADE_METRICS_0002_DDL)
    op.execute(DOWNGRADE_EVALUATE_MODEL_0011_DDL)
    op.execute(DOWNGRADE_LEADERBOARD_0005_DDL)
