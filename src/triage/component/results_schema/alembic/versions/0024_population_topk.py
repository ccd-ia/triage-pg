"""threshold metrics rank the full population (#21, ADR-0033)

DSSG triage ranked every scored test row, cut the list at k, and only then dropped the rows with
no label: ``precision@100_abs`` was "of the 100 entities we would act on, the share of the
labeled ones that turned out positive". ``labeled_ranks`` filters ``outcome is not null`` before
``row_number()``, and ``resolve_k`` turned ``N_pct`` into a count of labeled rows, so triage-pg
reported "the top 100 among the entities that have a label". The two agree when every test row
is labeled. In a selective-labels regime (``task_framing: resource_prioritization`` — an
inspection nobody made) they do not: DirtyDuck's test splits are 41–47% labeled.

This revision moves every threshold consumer onto DSSG's definition:

* ``triage.population_ranks`` — ``labeled_ranks`` with a LEFT join to the labels, so
  ``rank_abs`` ranks every scored row of the population (the subset, when one is given, unlabeled
  members included). The labels-artifact pin and its ambiguity check are the same.
* ``precision_at_k`` / ``recall_at_k`` — ``k`` resolves against the population size (``ceil``
  kept; DSSG used ``floor``). Precision is TP over the labeled rows inside the cut, stored in the
  new ``evaluations.num_labeled_above_threshold`` (DSSG's column of the same name); recall is TP
  over the labeled positives. ``value_expected``/``value_std`` are the exact random-ranking
  moments under the new sampling (``triage.precision_random_std``); ``value_worst``/``value_best``
  are the analytic extremes over the boundary tie block, which now holds unlabeled rows too.
* ``compute_bias_metrics`` — selection is the population top-k; ``group_size``, ``num_selected``
  and ``selection_rate`` count every row (as Aequitas did over DSSG's test frame); precision, FDR,
  FOR and NPV divide by labeled rows only.
* ``model_threshold_curve`` — the cut and ``pct`` over the population, precision over the labeled
  rows inside it, so the dashboard curve agrees with ``precision@x_pct`` again (0007 restored that
  agreement for the labeled-only definition).

Rank-free metrics (AUC, average precision, Brier, log loss, ECE, the regression metrics, the
C-index, calibration) need labeled rows only and stay on ``labeled_ranks``.

Every stored ``precision@``/``recall@`` row and every ``bias_metrics`` row is recomputed in place,
then the leaderboard is refreshed, so no table mixes the two definitions. A key whose predictions
carry no matrix lineage has no recoverable ``label_timespan`` and is left as it was; the NOTICE
counts them, and this lists them::

    select distinct e.model_id, e.split_kind, e.as_of_date
      from triage.evaluations e
     where e.metric in ('precision@', 'recall@') and e.num_labeled_above_threshold is null;

A recomputed bias row keeps the reference group it was stored with (passed as an explicit pin):
group sizes now include unlabeled rows, so "largest group" could otherwise change underneath it.

Downgrade restores the 0023 functions. It does not restore the old values — they are not kept.
Re-running an experiment after a downgrade recomputes them with the old definition.

Revision ID: 0024_population_topk
Revises: 0023_probability_metrics
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0024_population_topk"
down_revision = "0023_probability_metrics"
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


# --------------------------------------------------------------------------- upgrade DDL

SCHEMA_DDL = r"""
alter table triage.evaluations add column num_labeled_above_threshold integer;
comment on column triage.evaluations.num_labeled_above_threshold is
    'Labeled rows inside the top-k cut: the precision denominator. The cut ranks every scored '
    'row of the population (ADR-0033). NULL for metrics without a threshold.';

alter type triage.metric_result add attribute num_labeled_above_threshold integer;
"""

POPULATION_RANKS_DDL = r"""
-- labeled_ranks (0015) with a LEFT join to the labels: every scored row of the population, its
-- rank among all of them, and its label or NULL. The labels-artifact pin is labeled_ranks'.
create function triage.population_ranks(
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
        select distinct on (lp.entity_id)
               lp.entity_id, lp.score, lp.as_of_date
        from   triage.predictions lp
        where  lp.model_id   = p_model_id
          and  lp.split_kind = p_split_kind
          and  lp.as_of_date = p_as_of_date
        order  by lp.entity_id, lp.scored_at desc
    ),
    pop as (
        -- the population: the subset's members when one is given (0015 semantics)
        select p.entity_id, p.score, p.as_of_date
        from   p
        where  p_subset_hash = '' or exists (
                  select 1 from triage.subset_members sm
                  where sm.subset_hash = p_subset_hash
                    and sm.entity_id   = p.entity_id
                    and sm.as_of_date  = p.as_of_date)
    )
    select pop.entity_id,
           pop.score,
           row_number() over (order by pop.score desc, pop.entity_id) as rank_abs,
           l.outcome
    from   pop
    left join triage.labels l
           on  l.entity_id      = pop.entity_id
           and l.as_of_date     = pop.as_of_date
           and l.label_timespan = p_label_timespan
           and (v_pin is null or l.label_hash = v_pin);
end;
$$;

-- Standard deviation of precision@k under a random ranking of N rows, L of them labeled, P of
-- those positive. The labeled count in the cut is m ~ Hypergeom(N, L, k); given m, TP ~
-- Hypergeom(L, P, m), so E[TP/m | m] is the base rate b = P/L for every m and
--     Var = E_m[ b(1-b)(L-m) / ((L-1)·m) | m >= 1 ].
-- The pmf of m comes from its ratio recurrence in log space with a running log-sum-exp — no
-- lgamma (PostgreSQL has none before 18). With L = N it is the old closed form.
create function triage.precision_random_std(
    p_n integer, p_l integer, p_p integer, p_k integer
)
returns double precision
language plpgsql
immutable
as $$
declare
    b       double precision;
    m       integer;
    m_lo    integer := greatest(0, p_k - (p_n - p_l));
    m_hi    integer := least(p_k, p_l);
    lw      double precision := 0;   -- log of the unnormalised pmf at m
    mx      double precision := null;
    s_w     double precision := 0;   -- sum of exp(lw - mx) over m >= 1
    s_wf    double precision := 0;   -- the same, weighted by the conditional variance
    f       double precision;
begin
    if p_l < 2 or p_k < 1 or p_p <= 0 or p_p >= p_l or m_hi < 1 then
        return 0;
    end if;
    b := p_p::double precision / p_l;
    m := m_lo;
    loop
        if m >= 1 then
            f := b * (1 - b) * (p_l - m) / ((p_l - 1)::double precision * m);
            if mx is null or lw > mx then
                -- rescale the sums to the new maximum; exp() raises on underflow in
                -- PostgreSQL, and a weight below e^-700 of the maximum is 0 at this precision
                if mx is not null and mx - lw > -700 then
                    s_w  := s_w  * exp(mx - lw);
                    s_wf := s_wf * exp(mx - lw);
                else
                    s_w  := 0;
                    s_wf := 0;
                end if;
                mx := lw;
            end if;
            if lw - mx > -700 then
                s_w  := s_w  + exp(lw - mx);
                s_wf := s_wf + exp(lw - mx) * f;
            end if;
        end if;
        exit when m >= m_hi;
        lw := lw + ln((p_l - m)::double precision * (p_k - m))
                 - ln((m + 1)::double precision * (p_n - p_l - p_k + m + 1));
        m := m + 1;
    end loop;
    return sqrt(s_wf / s_w);
end;
$$;
"""

METRICS_DDL = r"""
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
    r          triage.metric_result;
    n_pop      integer;
    n_labeled  integer;
    n_positive integer;
    k          integer;
    m          integer;   -- labeled rows inside the cut: the precision denominator
    tp         integer;
    fixed_n    integer;   -- rows strictly above the cutoff score (always selected)
    fixed_lab  integer;
    fixed_pos  integer;
    tie_pos    integer;   -- the boundary tie block, by kind of row
    tie_neg    integer;
    tie_unl    integer;
    slots      integer;
    take_pos   integer;
    take_neg   integer;
    take_unl   integer;
begin
    select count(*)::int, count(outcome)::int,
           count(*) filter (where outcome > 0)::int
      into n_pop, n_labeled, n_positive
      from triage.population_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                   p_subset_hash);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_labeled = 0 then
        return r;  -- everything null but the counts
    end if;

    k := triage.resolve_k(p_parameter, n_pop);
    if k = 0 then
        r.value := null;
        return r;
    end if;

    with ranked as (
        select score, rank_abs, outcome
        from   triage.population_ranks(p_model_id, p_split_kind, p_as_of_date,
                                       p_label_timespan, p_subset_hash)
    ),
    cut as (select score as cutoff from ranked where rank_abs = k)
    select count(*) filter (where rank_abs <= k and outcome is not null)::int,
           count(*) filter (where rank_abs <= k and outcome > 0)::int,
           count(*) filter (where score > cutoff)::int,
           count(*) filter (where score > cutoff and outcome is not null)::int,
           count(*) filter (where score > cutoff and outcome > 0)::int,
           count(*) filter (where score = cutoff and outcome > 0)::int,
           count(*) filter (where score = cutoff and outcome <= 0)::int,
           count(*) filter (where score = cutoff and outcome is null)::int
      into m, tp, fixed_n, fixed_lab, fixed_pos, tie_pos, tie_neg, tie_unl
      from ranked, cut;

    r.num_labeled_above_threshold := m;
    r.value := case when m > 0 then tp::double precision / m end;

    -- Random ranking: E[precision | m >= 1] is the base rate; the spread is exact.
    r.value_expected := n_positive::double precision / n_labeled;
    r.value_std := triage.precision_random_std(n_pop, n_labeled, n_positive, k);

    -- Tie bounds: the extremes over orderings of the boundary tie block. A negative adds to the
    -- denominator without a TP; an unlabeled row adds to neither. Worst fills the free slots
    -- with negatives, then unlabeled rows, then positives; best with positives, then unlabeled
    -- rows, then negatives.
    slots := k - fixed_n;
    take_neg := least(slots, tie_neg);
    take_unl := least(slots - take_neg, tie_unl);
    take_pos := slots - take_neg - take_unl;
    r.value_worst := case when fixed_lab + take_neg + take_pos > 0
                          then (fixed_pos + take_pos)::double precision
                               / (fixed_lab + take_neg + take_pos) end;
    take_pos := least(slots, tie_pos);
    take_unl := least(slots - take_pos, tie_unl);
    take_neg := slots - take_pos - take_unl;
    r.value_best := case when fixed_lab + take_neg + take_pos > 0
                         then (fixed_pos + take_pos)::double precision
                              / (fixed_lab + take_neg + take_pos) end;

    return r;
end;
$$;

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
    r          triage.metric_result;
    n_pop      integer;
    n_labeled  integer;
    n_positive integer;
    k          integer;
    m          integer;
    tp         integer;
    fixed_n    integer;
    fixed_pos  integer;
    tie_n      integer;
    tie_pos    integer;
begin
    select count(*)::int, count(outcome)::int,
           count(*) filter (where outcome > 0)::int
      into n_pop, n_labeled, n_positive
      from triage.population_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan,
                                   p_subset_hash);

    r.num_labeled  := n_labeled;
    r.num_positive := n_positive;

    if n_labeled = 0 or n_positive = 0 then
        return r;  -- recall undefined with no positives
    end if;

    k := triage.resolve_k(p_parameter, n_pop);
    if k = 0 then
        r.value := 0;
        r.value_expected := 0;
        r.value_std := 0;
        return r;
    end if;

    with ranked as (
        select score, rank_abs, outcome
        from   triage.population_ranks(p_model_id, p_split_kind, p_as_of_date,
                                       p_label_timespan, p_subset_hash)
    ),
    cut as (select score as cutoff from ranked where rank_abs = k)
    select count(*) filter (where rank_abs <= k and outcome is not null)::int,
           count(*) filter (where rank_abs <= k and outcome > 0)::int,
           count(*) filter (where score > cutoff)::int,
           count(*) filter (where score > cutoff and outcome > 0)::int,
           count(*) filter (where score = cutoff)::int,
           count(*) filter (where score = cutoff and outcome > 0)::int
      into m, tp, fixed_n, fixed_pos, tie_n, tie_pos
      from ranked, cut;

    r.num_labeled_above_threshold := m;
    r.value := tp::double precision / n_positive;

    -- Random ranking: TP ~ Hypergeom(N, P, k), so E[recall] = k/N; std scales by 1/P.
    r.value_expected := k::double precision / n_pop;
    if n_pop > 1 then
        r.value_std := sqrt(k * (n_positive::double precision / n_pop)
                            * (1 - n_positive::double precision / n_pop)
                            * ((n_pop - k)::double precision / (n_pop - 1)))
                       / n_positive;
    else
        r.value_std := 0;
    end if;

    -- Tie bounds: only the positives taken from the tie block matter.
    r.value_worst := (fixed_pos + greatest(0, (k - fixed_n) - (tie_n - tie_pos)))::double precision
                     / n_positive;
    r.value_best  := (fixed_pos + least(tie_pos, k - fixed_n))::double precision / n_positive;

    return r;
end;
$$;
"""

BIAS_DDL = r"""
-- 0014's signature and output, over the population top-k (ADR-0033).
create or replace function triage.compute_bias_metrics(
    p_model_id        bigint,
    p_split_kind      triage.split_kind,
    p_as_of_date      date,
    p_label_timespan  interval,
    p_parameter       text,                      -- top-k threshold, e.g. '100_abs'
    p_ref_groups      jsonb default '{}'::jsonb, -- {"race": "White"} to pin reference; else largest group
    p_tau             double precision default 0.8 -- fairness threshold: pass = disparity in [tau, 1/tau]
)
returns integer
language plpgsql
volatile
as $$
declare
    n_pop      integer;
    k          integer;
    written    integer := 0;
begin
    select count(*)::int
      into n_pop
      from triage.population_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan);

    if n_pop = 0 then
        return 0;
    end if;
    k := triage.resolve_k(p_parameter, n_pop);

    -- Build the per-group metric table, then disparity vs the reference group,
    -- then unpivot to long format and upsert.
    with lr as (
        select entity_id, rank_abs, outcome
        from   triage.population_ranks(p_model_id, p_split_kind, p_as_of_date, p_label_timespan)
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
        -- group_size / num_selected count every row; tp/fp/fn/tn only labeled ones
        select attribute_name,
               attribute_value,
               count(*)::double precision                                          as group_size,
               count(*) filter (where selected)::double precision                  as num_selected,
               count(*) filter (where selected and outcome > 0)::double precision   as tp,
               count(*) filter (where selected and outcome = 0)::double precision   as fp,
               count(*) filter (where not selected and outcome > 0)::double precision as fn,
               count(*) filter (where not selected and outcome = 0)::double precision as tn,
               count(*) filter (where outcome > 0)::double precision               as group_pos,
               count(*) filter (where outcome = 0)::double precision               as group_neg
        from   joined
        group  by attribute_name, attribute_value
    ),
    metrics as (
        -- a rate that compares a prediction with a label divides by labeled rows only
        select attribute_name, attribute_value,
               group_size,
               num_selected,
               case when group_size > 0 then num_selected / group_size end as selection_rate,
               case when tp + fp     > 0 then tp / (tp + fp)   end         as precision_ppv,
               case when group_pos   > 0 then tp / group_pos   end         as tpr,
               case when group_neg   > 0 then fp / group_neg   end         as fpr,
               case when tp + fp     > 0 then fp / (tp + fp)   end         as fdr,
               case when group_pos   > 0 then fn / group_pos   end         as fnr,
               case when fn + tn     > 0 then fn / (fn + tn)   end         as for_rate,
               case when fn + tn     > 0 then tn / (fn + tn)   end         as npv
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
               m.fdr            as ref_fdr,
               m.fnr            as ref_fnr,
               m.for_rate       as ref_for,
               m.npv            as ref_npv
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
                 when x.metric = 'fnr'            and rv.ref_fnr is not null
                      and rv.ref_fnr <> 0 then x.value / rv.ref_fnr
                 when x.metric = 'for'            and rv.ref_for is not null
                      and rv.ref_for <> 0 then x.value / rv.ref_for
                 when x.metric = 'npv'            and rv.ref_npv is not null
                      and rv.ref_npv <> 0 then x.value / rv.ref_npv
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
                   ('fdr',            m.fdr),
                   ('fnr',            m.fnr),
                   ('for',            m.for_rate),
                   ('npv',            m.npv)
               ) as x(metric, value)
    )
    insert into triage.bias_metrics as b (
        model_id, split_kind, as_of_date, parameter,
        attribute_name, attribute_value, metric,
        value, ref_group_value, disparity, tau, passes_fairness, computed_at)
    select p_model_id, p_split_kind, p_as_of_date, p_parameter,
           attribute_name, attribute_value, metric,
           value, ref_value, disparity,
           case when disparity is not null then p_tau end,
           case when disparity is not null and p_tau is not null and p_tau > 0
                then disparity >= p_tau and disparity <= 1.0 / p_tau
           end,
           now()
    from   long
    on conflict (model_id, split_kind, as_of_date, parameter,
                 attribute_name, attribute_value, metric)
    do update set value = excluded.value,
                  ref_group_value = excluded.ref_group_value,
                  disparity = excluded.disparity,
                  tau = excluded.tau,
                  passes_fairness = excluded.passes_fairness,
                  computed_at = excluded.computed_at;

    get diagnostics written = row_count;
    return written;
end;
$$;
"""

CURVE_DDL = r"""
-- 0007's curve over the population: the cut and pct count every scored row, precision divides
-- by the labeled rows inside the cut, and fp/fn/tn count labeled rows — so the curve at x%
-- reads what precision@x_pct stores (ADR-0033).
create or replace function triage.model_threshold_curve(p_model bigint)
returns table(k integer, pct double precision, prec double precision, rec double precision,
              tp bigint, fp bigint, fn bigint, tn bigint)
language sql stable as $$
  with ranked as (
    select pr.rank_abs, l.outcome
    from   triage.prediction_ranks pr
    join   triage.models m on m.model_id = pr.model_id
    join   triage.run_artifacts ra on ra.run_id = m.run_id
    join   triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
    left join triage.labels l on l.label_hash = ra.artifact_id and l.entity_id = pr.entity_id
                             and l.as_of_date = pr.as_of_date
                             and l.label_timespan = m.training_label_timespan
    where  pr.model_id = p_model and pr.split_kind = 'test'),
  ordered as (
    select outcome, row_number() over (order by rank_abs) as flagged
    from   ranked),
  tot as (
    select count(*) as n, count(outcome) as l, coalesce(sum(outcome), 0) as p from ranked),
  cum as (
    select flagged,
           coalesce(sum(outcome) over (order by flagged), 0) as tp,
           count(outcome) over (order by flagged)            as lab
    from   ordered)
  select cum.flagged::int                                   as k,
         cum.flagged::double precision / nullif(tot.n, 0)   as pct,
         cum.tp / nullif(cum.lab, 0)                        as prec,
         cum.tp / nullif(tot.p, 0)                          as rec,
         cum.tp::bigint                                     as tp,
         (cum.lab - cum.tp)::bigint                         as fp,
         (tot.p - cum.tp)::bigint                           as fn,
         (tot.l - tot.p - (cum.lab - cum.tp))::bigint       as tn
  from   cum cross join tot
  order by cum.flagged;
$$;
"""

# Recompute every stored threshold and bias row with the functions above.
RECOMPUTE_DDL = r"""
do $$
declare
    key        record;
    ev         record;
    ts         interval;
    n_ts       integer;
    res        triage.metric_result;
    n_rows     integer := 0;
    n_bias     integer := 0;
    n_skipped  integer := 0;
begin
    for key in
        select distinct model_id, split_kind, as_of_date, subset_hash
          from triage.evaluations
         where metric in ('precision@', 'recall@')
    loop
        -- the label_timespan the scored matrix was built with
        select count(distinct mx.label_timespan), min(mx.label_timespan)
          into n_ts, ts
          from triage.predictions pr
          join triage.matrices mx on mx.matrix_uuid = pr.matrix_uuid
         where pr.model_id   = key.model_id
           and pr.split_kind = key.split_kind
           and pr.as_of_date = key.as_of_date;
        if n_ts <> 1 then
            n_skipped := n_skipped + 1;
            continue;
        end if;
        for ev in
            select e.metric, e.parameter
              from triage.evaluations e
             where e.model_id = key.model_id and e.split_kind = key.split_kind
               and e.as_of_date = key.as_of_date and e.subset_hash = key.subset_hash
               and e.metric in ('precision@', 'recall@')
        loop
            if ev.metric = 'precision@' then
                res := triage.precision_at_k(key.model_id, key.split_kind, key.as_of_date, ts,
                                             ev.parameter, key.subset_hash);
            else
                res := triage.recall_at_k(key.model_id, key.split_kind, key.as_of_date, ts,
                                          ev.parameter, key.subset_hash);
            end if;
            update triage.evaluations e
               set value = res.value,
                   value_worst = res.value_worst,
                   value_best = res.value_best,
                   value_expected = res.value_expected,
                   value_std = res.value_std,
                   num_labeled = res.num_labeled,
                   num_positive = res.num_positive,
                   num_labeled_above_threshold = res.num_labeled_above_threshold,
                   computed_at = now()
             where e.model_id = key.model_id and e.split_kind = key.split_kind
               and e.as_of_date = key.as_of_date and e.subset_hash = key.subset_hash
               and e.metric = ev.metric and e.parameter = ev.parameter;
            n_rows := n_rows + 1;
        end loop;
    end loop;

    for key in
        select b.model_id, b.split_kind, b.as_of_date, b.parameter,
               coalesce(jsonb_object_agg(b.attribute_name, b.ref_group_value)
                        filter (where b.ref_group_value is not null), '{}'::jsonb) as pins,
               max(b.tau)            as tau,
               bool_and(b.tau is null) as tau_was_null
          from (select distinct model_id, split_kind, as_of_date, parameter,
                                attribute_name, ref_group_value, tau
                  from triage.bias_metrics) b
         group by b.model_id, b.split_kind, b.as_of_date, b.parameter
    loop
        select count(distinct mx.label_timespan), min(mx.label_timespan)
          into n_ts, ts
          from triage.predictions pr
          join triage.matrices mx on mx.matrix_uuid = pr.matrix_uuid
         where pr.model_id   = key.model_id
           and pr.split_kind = key.split_kind
           and pr.as_of_date = key.as_of_date;
        if n_ts <> 1 then
            n_skipped := n_skipped + 1;
            continue;
        end if;
        delete from triage.bias_metrics b
         where b.model_id = key.model_id and b.split_kind = key.split_kind
           and b.as_of_date = key.as_of_date and b.parameter = key.parameter;
        perform triage.compute_bias_metrics(key.model_id, key.split_kind, key.as_of_date, ts,
                                            key.parameter, key.pins, coalesce(key.tau, 0.8));
        if key.tau_was_null then  -- pre-0014 rows applied no tau; keep saying so
            update triage.bias_metrics b
               set tau = null, passes_fairness = null
             where b.model_id = key.model_id and b.split_kind = key.split_kind
               and b.as_of_date = key.as_of_date and b.parameter = key.parameter;
        end if;
        n_bias := n_bias + 1;
    end loop;

    raise notice 'migration 0024: recomputed % threshold evaluation row(s) and % bias key(s);'
        ' % key(s) without matrix lineage left as they were', n_rows, n_bias, n_skipped;
end;
$$;

refresh materialized view triage.leaderboard;
"""


def _evaluate_model_0023() -> str:
    return _revision_module("0023_probability_metrics")._evaluate_model_probability()


def _evaluate_model_0024() -> str:
    """0023's evaluate_model with num_labeled_above_threshold written by the threshold branch.

    The threshold branch is the only one indented this deep, so each anchor occurs once.
    """
    body = _evaluate_model_0023()
    body = _replace_once(
        body,
        "                    num_labeled, num_positive, computed_at)\n                values (\n",
        "                    num_labeled, num_positive, num_labeled_above_threshold,"
        + " computed_at)\n                values (\n",
    )
    body = _replace_once(
        body,
        "                    res.num_labeled, res.num_positive, now())\n",
        "                    res.num_labeled, res.num_positive,"
        + " res.num_labeled_above_threshold, now())\n",
    )
    return _replace_once(
        body,
        "                              num_positive = excluded.num_positive,\n"
        + "                              computed_at = excluded.computed_at;\n",
        "                              num_positive = excluded.num_positive,\n"
        + "                              num_labeled_above_threshold ="
        + " excluded.num_labeled_above_threshold,\n"
        + "                              computed_at = excluded.computed_at;\n",
    )


def upgrade() -> None:
    op.execute(SCHEMA_DDL)
    op.execute(POPULATION_RANKS_DDL)
    op.execute(METRICS_DDL)
    op.execute(_evaluate_model_0024())
    op.execute(BIAS_DDL)
    op.execute(CURVE_DDL)
    op.execute(RECOMPUTE_DDL)


# ------------------------------------------------------------------------------ downgrade


def _metrics_0015() -> str:
    """0015's precision_at_k and recall_at_k — only those two: re-running all of its
    METRICS_DDL would also put back 0015's regression_metric and erase 0020's pinball."""
    return _between(
        _revision_module("0015_subset_evaluation").METRICS_DDL,
        "drop function if exists triage.precision_at_k(",
        "drop function if exists triage.auc_roc(",
    )


def _bias_0014() -> str:
    return _between(
        _revision_module("0014_bias_completeness").UPGRADE_DDL,
        "create or replace function triage.compute_bias_metrics(",
    )


def downgrade() -> None:
    op.execute(_evaluate_model_0023())
    op.execute(_metrics_0015())
    op.execute(_bias_0014())
    op.execute(_revision_module("0007_rayid_curve_labeled").CURVE_LABELED)
    op.execute(
        "drop function triage.precision_random_std(integer, integer, integer, integer);"
        + " drop function triage.population_ranks("
        + "bigint, triage.split_kind, date, interval, text);"
        + " alter type triage.metric_result drop attribute num_labeled_above_threshold;"
        + " alter table triage.evaluations drop column num_labeled_above_threshold;"
        + " refresh materialized view triage.leaderboard;"
    )
