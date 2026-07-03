"""survival C-index + lineage-pinned evaluation joins (ADR-0010/0026)

Revision ID: 0011_survival_c_index
Revises: 0010_windowed_evaluations
Create Date: 2026-07-03

Two things land here, both found running the first live survival E2E:

1. **The survival metric.** ADR-0010 made the label schema survival-ready (nullable
   ``duration``/``event_observed``) and deferred the metric. ``triage.c_index`` computes
   Harrell's concordance in-PG (ADR-0007): semantics match
   ``sksurv.metrics.concordance_index_censored`` (comparable pairs: earlier event vs any
   later time, or equal-time event-vs-censored; tied risk = 0.5; equal-time event/event
   pairs excluded), cross-checked by test. The pair join runs over a MATERIALIZED CTE —
   joining a set-returning SQL function to itself lets the planner inline + re-execute the
   whole underlying view per outer row (observed live: 15+ minutes for a 2k-row date).

2. **Label-artifact pinning.** ``labeled_ranks`` (0002) joined ``triage.labels`` by
   ``(entity, date, timespan)`` only — sound while a project DB holds ONE labels artifact
   per slice, silently double-joined the moment two artifacts cover the same slice
   (observed live: duplicated label rows doubled ``num_labeled`` and quadrupled the pair
   count). Both ranks functions now resolve the labels artifact each prediction's MATRIX
   was built from (predictions.matrix_uuid → matrices.artifact_id → artifact_inputs →
   the ``labels``-kind parent) and pin the join to it; predictions without matrix lineage
   (seeded fixtures, forward scores) keep the unpinned join. More than one resolved labels
   parent raises (genuine ambiguity must be loud, never averaged over).

``evaluate_model`` is CREATE OR REPLACEd with a ``survival_metrics`` loop added; the
downgrade restores the 0002 bodies verbatim and drops the two new functions.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_survival_c_index"
down_revision = "0010_windowed_evaluations"
branch_labels = None
depends_on = None


RANKS_AND_SURVIVAL_DDL = r"""
-- The entity-date labels join needs its own index: the labels PK leads with label_hash,
-- so (entity, date) lookups otherwise fall to seq scans — pathological once several label
-- artifacts coexist (observed live: minutes per metric call on a ~100k-row labels table).
create index if not exists labels_entity_date_idx
    on triage.labels (entity_id, as_of_date, label_timespan);

-- ------------------------------------------------ labeled_ranks (pinned)
-- The labeled, ranked working set for one (model, split, as_of_date, label_timespan),
-- with the labels join PINNED to the labels artifact the scored matrix was built from.
-- plpgsql on purpose: the pin resolves into a VARIABLE first — embedding it as a scalar
-- subquery in the join condition defeats hash-join planning (observed live: nested-loop
-- seq scans, minutes per call), and >1 candidate artifacts must raise, never mix.
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
           -- re-rank within the *labeled* subset so top-k is contiguous even when some
           -- ranked rows lack a label (monitoring case).
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

-- ------------------------------------------------ survival_ranks (pinned)
-- The labeled survival working set: rows carrying the (duration, event_observed) pair
-- (ADR-0010 survival projection), same pinning discipline as labeled_ranks.
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

-- ------------------------------------------------ Harrell's C (survival)
-- value ∈ [0,1]; 0.5 = random ranking. num_positive carries the EVENT count
-- (events play the "positive" role on the ranking spine).
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
        return r;  -- C undefined with no comparable pairs possible
    end if;

    -- MATERIALIZED is load-bearing: self-joining the set-returning function directly lets
    -- the planner inline it and re-execute the whole underlying scan per outer row.
    -- Risk-score ties use sksurv's tolerance (concordance_index_censored tied_tol=1e-8):
    -- |Δscore| <= 1e-8 is a tie (0.5), concordant needs Δscore > 1e-8 — exact-equality
    -- ties diverge from the reference on continuous scores (observed live at ~1e-6).
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

# evaluate_model with the survival_metrics loop added (everything above the survival
# block is the 0002 body, unchanged).
EVALUATE_MODEL_WITH_SURVIVAL_DDL = r"""
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

# The 0002 bodies, verbatim, for the downgrade.
LABELED_RANKS_ORIGINAL_DDL = r"""
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
language sql
stable
as $$
    select pr.entity_id,
           pr.score,
           row_number() over (order by pr.score desc, pr.entity_id) as rank_abs,
           l.outcome
    from   triage.prediction_ranks pr
    join   triage.labels l
           on  l.entity_id      = pr.entity_id
           and l.as_of_date     = pr.as_of_date
           and l.label_timespan = p_label_timespan
    where  pr.model_id   = p_model_id
      and  pr.split_kind = p_split_kind
      and  pr.as_of_date = p_as_of_date
      and  l.outcome is not null;
$$;
"""

EVALUATE_MODEL_ORIGINAL_DDL = r"""
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

    return written;
end;
$$;
"""


def upgrade():
    op.execute(RANKS_AND_SURVIVAL_DDL)
    op.execute(EVALUATE_MODEL_WITH_SURVIVAL_DDL)


def downgrade():
    op.execute(EVALUATE_MODEL_ORIGINAL_DDL)
    op.execute(LABELED_RANKS_ORIGINAL_DDL)
    op.execute(r"""
        drop function if exists triage.c_index(bigint, triage.split_kind, date, interval);
        drop function if exists triage.survival_ranks(bigint, triage.split_kind, date, interval);
        drop index if exists triage.labels_entity_date_idx;
        """)
