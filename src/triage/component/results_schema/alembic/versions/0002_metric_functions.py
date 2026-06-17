"""in-PG metric functions + SQL bias group-bys (ADR-0007)

Revision ID: 0002_metric_functions
Revises: 0001_initial_triage_schema
Create Date: 2026-06-16

In-Postgres evaluation (ADR-0007): PL/pgSQL functions + SQL group-bys that
populate ``triage.evaluations`` and ``triage.bias_metrics``, replacing the
dropped Python/sklearn + Aequitas path. Metrics need only ``(entity_id, score,
label)`` — all of which live in PostgreSQL regardless of where matrices are
stored — so they run as set-based SQL over ``triage.prediction_ranks`` joined to
``triage.labels``.

LABEL-JOIN DECISION
-------------------
``predictions`` carries no label column (schema-design §8, decision 1: labels
JOIN, not denormalize — in monitoring the outcome arrives *after* scoring, so
denormalizing would force backfilling append-only rows). The functions therefore
join the *latest-score* ranking view to the labels table:

    triage.prediction_ranks  ⋈  triage.labels
        ON (entity_id, as_of_date)  AND  labels.label_timespan = <param>

We key on ``(entity_id, as_of_date, label_timespan)`` rather than routing through
``predictions.matrix_uuid → matrices → train labels``, because a model's
``train_matrix_uuid`` describes the labels it was *trained* on, not the
test/production labels being *scored* here. Keying on the scored entity-dates is
correct for both the experiment case (label already present) and the monitoring
case (label arrives later) with a single code path — exactly the §8 rationale.
``label_timespan`` is an explicit function argument; ``subset_hash`` is recorded
on the rows but subset *filtering* is deferred (schema-design §8, decision 6).

METRICS (organized by problem_type, ADR-0010)
---------------------------------------------
Classification ranking metrics over ``prediction_ranks ⋈ labels`` (labeled rows
only; deterministic ties already resolved by the view's
``order by score desc, entity_id``):

  * ``precision@`` / ``recall@`` for k as absolute (top-N by ``rank_abs``) and as
    percentage (``rank_pct``). Returns the deterministic realized value plus the
    analytic random-ranking baseline (``value_expected`` / ``value_std``,
    hypergeometric) and the deterministic worst/best-case tie bounds.
  * ``auc_roc`` computed EXACTLY via the Mann-Whitney rank-sum identity:
        auc = (sum(rank_of_positive) - n_pos*(n_pos+1)/2) / (n_pos * n_neg)
    No sampling. Mid-ranks handle score ties.
  * ``average_precision`` (area under the PR curve) via cumulative TP / positives
    with window functions.

Regression metrics (``score`` = prediction, ``outcome`` = actual):
  * ``rmse`` / ``mae`` / ``r2``.

Analytic baselines (schema-design §8.3, hypergeometric — NOT Monte Carlo): for
precision@k under random ranking, ``value_expected = K/N`` (the base rate) and
``value_std = sqrt(k*(K/N)*(1-K/N)*((N-k)/(N-1)))/k`` (population K positives of N
labeled, k selected). ``value_worst`` / ``value_best`` hold the deterministic
worst/best-case tie realizations at the top-k boundary; null where not
meaningful (and for scalar metrics auc_roc / average_precision / regression).

BIAS (ADR-0007: Aequitas dropped → SQL group-bys)
-------------------------------------------------
``triage.compute_bias_metrics`` group-bys ``protected_groups ⋈ prediction_ranks``
computing, per ``(attribute_name, attribute_value)`` group at a top-k threshold:
group_size, selected count, selection rate (PPV proxy), and label-aware
precision/tpr/fpr/fdr; plus disparity = group value ÷ reference-group value (the
largest group by default, or a caller-supplied reference). Long-format rows land
in ``triage.bias_metrics`` — replacing the 50-column Aequitas dump.

DISPATCHER
----------
``triage.evaluate_model(model_id, split_kind, as_of_date, label_timespan,
metric_config jsonb, subset_hash)`` drives the per-metric functions and does
idempotent ``INSERT … ON CONFLICT DO UPDATE`` into ``triage.evaluations``.
``triage.evaluate_model_bias(...)`` is the bias analog into
``triage.bias_metrics``.

Raw SQL in ``op.execute`` on purpose, mirroring 0001's style (PL/pgSQL bodies do
not round-trip through SQLAlchemy).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_metric_functions"
down_revision = "0001_initial_triage_schema"
branch_labels = None
depends_on = None


FUNCTIONS_DDL = r"""
-- ====================================================================
-- ADR-0007 in-PG evaluation: PL/pgSQL metric functions over
-- triage.prediction_ranks ⋈ triage.labels, plus SQL bias group-bys.
-- All functions live in the `triage` schema and are STABLE (read-only) /
-- VOLATILE only where they INSERT.
-- ====================================================================

-- A composite return type for the threshold-metric family so a single
-- function call yields every column the evaluations table needs.
create type triage.metric_result as (
    value          double precision,
    value_worst    double precision,
    value_best     double precision,
    value_expected double precision,
    value_std      double precision,
    num_labeled    integer,
    num_positive   integer
);

-- ---------------------------------------------------------------- helpers
-- Resolve the absolute top-k cutoff from a parameter:
--   '100_abs' -> 100             (absolute count)
--   '10_pct'  -> ceil(0.10 * N)  (percentage of the labeled set)
-- Returns the integer number of rows to select, clamped to [0, N].
create or replace function triage.resolve_k(parameter text, n_labeled integer)
returns integer
language plpgsql
immutable
as $$
declare
    raw      text;
    unit     text;
    k        integer;
begin
    if parameter is null or position('_' in parameter) = 0 then
        raise exception 'malformed threshold parameter %, expected "<n>_abs" or "<n>_pct"', parameter;
    end if;
    raw  := split_part(parameter, '_', 1);
    unit := split_part(parameter, '_', 2);
    if unit = 'abs' then
        k := floor(raw::numeric)::integer;
    elsif unit = 'pct' then
        k := ceil((raw::numeric / 100.0) * n_labeled)::integer;
    else
        raise exception 'unknown threshold unit % (expected abs|pct)', unit;
    end if;
    if k < 0 then
        k := 0;
    elsif k > n_labeled then
        k := n_labeled;
    end if;
    return k;
end;
$$;

-- The labeled, ranked working set for one (model, split, as_of_date,
-- label_timespan). Deterministic ranks come straight from the view; the join to
-- labels is INNER, so only labeled rows participate (matches the Python path,
-- which dropped unlabeled rows before scoring).
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
           -- re-rank within the *labeled* subset so top-k is contiguous even
           -- when some ranked rows lack a label (monitoring case).
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

-- ------------------------------------------------ precision@k (classification)
-- Returns the deterministic realized precision plus analytic random-ranking
-- baseline and deterministic worst/best tie bounds.
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
    tp          integer;          -- realized true positives in the deterministic top-k
    base_rate   double precision; -- K/N
    worst_tp    integer;
    best_tp     integer;
begin
    select count(*)::int, coalesce(sum((outcome > 0)::int), 0)::int
      into n_labeled, n_positive
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

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
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
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
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
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

-- ------------------------------------------------ recall@k (classification)
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
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
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

-- ------------------------------------------------ AUC-ROC (exact, Mann-Whitney)
-- auc = (sum(rank_of_positive) - n_pos*(n_pos+1)/2) / (n_pos * n_neg).
-- rank() with mid-ranks for score ties keeps the identity exact under ties.
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
        return r;  -- AUC undefined when one class is empty
    end if;

    -- mid-rank ascending by score; average rank within score ties.
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

-- ------------------------------------------------ average precision (PR-AUC)
-- AP = sum over positives of precision-at-that-positive, divided by n_positive.
-- Uses cumulative TP / cumulative selected with the deterministic rank order.
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

-- ------------------------------------------------ regression metrics
-- RMSE / MAE / R² over (score = prediction, outcome = actual). label_timespan
-- still keys the join; outcome is the continuous target (ADR-0010).
create or replace function triage.regression_metric(
    p_model_id       bigint,
    p_split_kind     triage.split_kind,
    p_as_of_date     date,
    p_label_timespan interval,
    p_metric         text          -- 'rmse' | 'mae' | 'r2'
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

-- ------------------------------------------------ dispatcher into evaluations
-- metric_config jsonb shape:
--   {
--     "thresholds": ["100_abs", "10_pct"],   -- for precision@/recall@
--     "metrics":    ["precision@","recall@","auc_roc","average_precision"],
--     "regression_metrics": ["rmse","mae","r2"]   -- optional
--   }
-- Idempotent: ON CONFLICT on the evaluations PK updates value columns +
-- computed_at, so re-evaluating a (model, split, date, subset, metric, param)
-- overwrites in place.
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
    -- v_-prefixed to avoid colliding with the unqualified `metric` / `parameter`
    -- column names in the INSERT below (PL/pgSQL would otherwise raise ambiguity).
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

    return written;
end;
$$;

-- ====================================================================
-- Bias / fairness as SQL group-bys (ADR-0007, Aequitas dropped).
-- Per (attribute_name, attribute_value) group at one top-k threshold:
--   group_size, num_selected, selection_rate (= num_selected/group_size),
--   precision (PPV among selected), tpr, fpr, fdr.
-- disparity = group value ÷ reference-group value. Reference = the largest
-- group for the attribute, unless an explicit ref value is supplied.
-- Long-format rows into triage.bias_metrics, idempotent on its PK.
-- ====================================================================
create or replace function triage.compute_bias_metrics(
    p_model_id        bigint,
    p_split_kind      triage.split_kind,
    p_as_of_date      date,
    p_label_timespan  interval,
    p_parameter       text,                     -- top-k threshold, e.g. '100_abs'
    p_ref_groups      jsonb default '{}'::jsonb -- {"race": "White"} to pin reference; else largest group
)
returns integer
language plpgsql
volatile
as $$
declare
    n_labeled  integer;
    k          integer;
    written    integer := 0;
begin
    select count(*)::int
      into n_labeled
      from triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    if n_labeled = 0 then
        return 0;
    end if;
    k := triage.resolve_k(p_parameter, n_labeled);

    -- Build the per-group metric table, then disparity vs the reference group,
    -- then unpivot to long format and upsert.
    with lr as (
        select entity_id, rank_abs, outcome
        from   triage.labeled_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
    ),
    joined as (
        select pg.attribute_name,
               pg.attribute_value,
               lr.outcome,
               (lr.rank_abs <= k) as selected
        from   lr
        join   triage.protected_groups pg
               on  pg.entity_id  = lr.entity_id
               and pg.as_of_date = p_as_of_date
    ),
    grouped as (
        select attribute_name,
               attribute_value,
               count(*)::double precision                                          as group_size,
               count(*) filter (where selected)::double precision                  as num_selected,
               count(*) filter (where selected and outcome > 0)::double precision   as tp,
               count(*) filter (where selected and outcome = 0)::double precision   as fp,
               count(*) filter (where not selected and outcome > 0)::double precision as fn,
               count(*) filter (where outcome > 0)::double precision               as group_pos,
               count(*) filter (where outcome = 0)::double precision               as group_neg
        from   joined
        group  by attribute_name, attribute_value
    ),
    metrics as (
        select attribute_name, attribute_value,
               group_size,
               num_selected,
               case when group_size > 0 then num_selected / group_size end as selection_rate,
               case when num_selected > 0 then tp / num_selected end       as precision_ppv,
               case when group_pos   > 0 then tp / group_pos   end         as tpr,
               case when group_neg   > 0 then fp / group_neg   end         as fpr,
               case when num_selected > 0 then fp / num_selected end       as fdr
        from   grouped
    ),
    -- reference group per attribute: explicit pin, else largest group_size
    refs as (
        select distinct on (m.attribute_name)
               m.attribute_name,
               coalesce(p_ref_groups->>m.attribute_name,
                        first_value(m.attribute_value) over (
                            partition by m.attribute_name
                            order by m.group_size desc, m.attribute_value)) as ref_value
        from   metrics m
        order  by m.attribute_name
    ),
    ref_vals as (
        select m.attribute_name,
               r.ref_value,
               m.selection_rate as ref_selection_rate,
               m.precision_ppv  as ref_precision,
               m.tpr            as ref_tpr,
               m.fpr            as ref_fpr,
               m.fdr            as ref_fdr
        from   metrics m
        join   refs r on r.attribute_name = m.attribute_name
                     and m.attribute_value = r.ref_value
    ),
    long as (
        -- one row per (group, metric); disparity computed against the matching ref
        select m.attribute_name, m.attribute_value, x.metric, x.value,
               rv.ref_value,
               case
                 when x.metric = 'group_size'      then null  -- count, no disparity
                 when x.metric = 'num_selected'    then null
                 when x.metric = 'selection_rate' and rv.ref_selection_rate is not null
                      and rv.ref_selection_rate <> 0 then x.value / rv.ref_selection_rate
                 when x.metric = 'precision'      and rv.ref_precision is not null
                      and rv.ref_precision <> 0 then x.value / rv.ref_precision
                 when x.metric = 'tpr'            and rv.ref_tpr is not null
                      and rv.ref_tpr <> 0 then x.value / rv.ref_tpr
                 when x.metric = 'fpr'            and rv.ref_fpr is not null
                      and rv.ref_fpr <> 0 then x.value / rv.ref_fpr
                 when x.metric = 'fdr'            and rv.ref_fdr is not null
                      and rv.ref_fdr <> 0 then x.value / rv.ref_fdr
                 else null
               end as disparity
        from   metrics m
        join   ref_vals rv on rv.attribute_name = m.attribute_name
        cross  join lateral (values
                   ('group_size',     m.group_size),
                   ('num_selected',   m.num_selected),
                   ('selection_rate', m.selection_rate),
                   ('precision',      m.precision_ppv),
                   ('tpr',            m.tpr),
                   ('fpr',            m.fpr),
                   ('fdr',            m.fdr)
               ) as x(metric, value)
    )
    insert into triage.bias_metrics as b (
        model_id, split_kind, as_of_date, parameter,
        attribute_name, attribute_value, metric,
        value, ref_group_value, disparity, computed_at)
    select p_model_id, p_split_kind, p_as_of_date, p_parameter,
           attribute_name, attribute_value, metric,
           value, ref_value, disparity, now()
    from   long
    on conflict (model_id, split_kind, as_of_date, parameter,
                 attribute_name, attribute_value, metric)
    do update set value = excluded.value,
                  ref_group_value = excluded.ref_group_value,
                  disparity = excluded.disparity,
                  computed_at = excluded.computed_at;

    get diagnostics written = row_count;
    return written;
end;
$$;
"""


# Drop in reverse dependency order. The composite type is used by the
# threshold/scalar functions, so functions go first, then the type.
DROP_DDL = r"""
drop function if exists triage.compute_bias_metrics(bigint, triage.split_kind, date, interval, text, jsonb);
drop function if exists triage.evaluate_model(bigint, triage.split_kind, date, interval, jsonb, text);
drop function if exists triage.regression_metric(bigint, triage.split_kind, date, interval, text);
drop function if exists triage.average_precision(bigint, triage.split_kind, date, interval);
drop function if exists triage.auc_roc(bigint, triage.split_kind, date, interval);
drop function if exists triage.recall_at_k(bigint, triage.split_kind, date, interval, text);
drop function if exists triage.precision_at_k(bigint, triage.split_kind, date, interval, text);
drop function if exists triage.labeled_ranks(bigint, triage.split_kind, date, interval);
drop function if exists triage.resolve_k(text, integer);
drop type if exists triage.metric_result;
"""


def upgrade():
    op.execute(FUNCTIONS_DDL)


def downgrade():
    op.execute(DROP_DDL)
