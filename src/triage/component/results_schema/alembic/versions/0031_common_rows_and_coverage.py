"""coverage, and comparing model groups on the rows every one of them scored

A baseline that has no target history to forecast from abstains: the row gets no prediction
(``prediction_ranking._finite_or_none``). Each group is then evaluated on its own rows, so a
group that skipped the hard rows can rank above one that scored them all. This revision makes
the difference visible and lets audition compare on equal rows.

* ``label_source(model, split)`` — per scored date, the label horizon and labels artifact the
  scores are labeled with: the scored matrix's lineage, else the training horizon and the run's
  labels artifact (``scored_labels``' rule, 0026).
* ``scored_coverage(groups, split)`` — per group, model and date: the labeled rows of the date
  (``n_labeled``) and those the model scored (``n_scored``). Coverage is their ratio.
* ``model_group_coverage(experiment, split)`` — per group: the mean coverage over its
  model-dates, and the lowest.
* ``common_rows_metrics(groups, split, as_of_date)`` — per group and date: the own and common
  row counts, MAE on the group's own rows, and MAE, RMSE, WAPE and bias (mean of ŷ − y) on the
  common rows: the labeled rows that every listed group scored on that date. A date some listed
  group did not score has no common rows. Computed on read.
* ``materialize_common_rows(groups, split)`` — registers the common rows as a Subset,
  ``'common:' || md5(sorted ids, split)``, and refills its members. Idempotent. Evaluating on
  it uses the subset machinery that exists (0015): the subset is the population.
* ``audition_windowed``, ``audition_pick`` and ``selected_model`` gain ``p_subset_hash text
  default ''`` and read ``audition_distances_by_subset``, the 0013 view keyed by subset. The
  ``audition_distances`` view keeps its columns and reads that view at ``subset_hash = ''``, so
  every existing call returns what it returned before.

Downgrade restores the 0022 functions and the 0013 view and drops the rest.

Revision ID: 0031_common_rows_and_coverage
Revises: 0030_wape_and_forecast_quantile
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "0031_common_rows_and_coverage"
down_revision = "0030_wape_and_forecast_quantile"
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


def _replace(text: str, old: str, new: str, count: int) -> str:
    """``text`` with ``old`` replaced by ``new``; ``old`` must occur exactly ``count`` times."""
    if text.count(old) != count:
        raise RuntimeError(
            f"{old[:60]!r} occurs {text.count(old)} times in the 0022 text, expected {count}"
        )
    return text.replace(old, new)


# ------------------------------------------------------------------- coverage, common rows

COMMON_ROWS_DDL = r"""
-- Per scored date of a model: the label horizon and labels artifact its scores are labeled
-- with. The scored matrix's lineage, else the training horizon and the run's labels artifact
-- (scored_labels, 0026). A date that mixes two horizons or two artifacts raises.
create function triage.label_source(
    p_model      bigint,
    p_split_kind triage.split_kind default 'test'
)
returns table (as_of_date date, label_timespan interval, label_hash text)
language plpgsql
stable
as $$
#variable_conflict use_column
declare
    d          record;
    v_run_pin  text;
    v_training interval;
begin
    select (select min(ra.artifact_id)
              from triage.run_artifacts ra
              join triage.artifacts a on a.artifact_id = ra.artifact_id and a.kind = 'labels'
             where ra.run_id = m.run_id),
           m.training_label_timespan
      into v_run_pin, v_training
      from triage.models m
     where m.model_id = p_model;

    for d in
        select pr.as_of_date,
               count(distinct mx.label_timespan) as n_ts,
               min(mx.label_timespan)            as ts,
               count(distinct la.artifact_id)    as n_pins,
               min(la.artifact_id)               as pin
          from triage.predictions pr
          left join triage.matrices        mx on mx.matrix_uuid = pr.matrix_uuid
          left join triage.artifact_inputs ai on ai.artifact_id = mx.artifact_id
          left join triage.artifacts       la on la.artifact_id = ai.parent_id
                                             and la.kind = 'labels'
         where pr.model_id = p_model and pr.split_kind = p_split_kind
         group by pr.as_of_date
         order by pr.as_of_date
    loop
        if d.n_ts > 1 or d.n_pins > 1 then
            raise exception 'model % at %: the % predictions span % label timespans and % labels'
                ' artifacts — refusing to mix label definitions',
                p_model, d.as_of_date, p_split_kind, d.n_ts, d.n_pins;
        end if;
        as_of_date     := d.as_of_date;
        label_timespan := coalesce(d.ts, v_training);
        label_hash     := coalesce(d.pin, v_run_pin);
        return next;
    end loop;
end;
$$;

-- Per group, model and scored date: the labeled rows of the date, and those the model scored.
create function triage.scored_coverage(
    p_model_group_ids bigint[],
    p_split_kind      triage.split_kind default 'test'
)
returns table (model_group_id bigint, model_id bigint, as_of_date date,
               n_labeled bigint, n_scored bigint)
language sql
stable
as $$
    with src as (
        select m.model_group_id, m.model_id, ls.as_of_date, ls.label_timespan, ls.label_hash
        from   triage.models m
        cross  join lateral triage.label_source(m.model_id, p_split_kind) ls
        where  m.model_group_id = any(p_model_group_ids)
    )
    select src.model_group_id, src.model_id, src.as_of_date,
           (select count(*) from triage.labels l
             where l.as_of_date = src.as_of_date
               and l.label_timespan = src.label_timespan
               and l.label_hash = src.label_hash
               and l.outcome is not null),
           (select count(*) from triage.latest_predictions p
              join triage.labels l
                on l.entity_id = p.entity_id and l.as_of_date = p.as_of_date
               and l.label_timespan = src.label_timespan and l.label_hash = src.label_hash
             where p.model_id = src.model_id and p.split_kind = p_split_kind
               and p.as_of_date = src.as_of_date and l.outcome is not null)
    from src;
$$;

-- Per model group of an Experiment: the mean coverage over its model-dates, and the lowest.
create function triage.model_group_coverage(
    p_experiment text,
    p_split_kind triage.split_kind default 'test'
)
returns table (model_group_id bigint, n_dates bigint, coverage double precision,
               min_coverage double precision)
language sql
stable
as $$
    select c.model_group_id,
           count(*),
           avg(c.n_scored::double precision / nullif(c.n_labeled, 0)),
           min(c.n_scored::double precision / nullif(c.n_labeled, 0))
    from triage.scored_coverage(
             array(select distinct m.model_group_id
                     from triage.models m
                     join triage.runs r on r.run_id = m.run_id
                    where r.experiment_hash = p_experiment),
             p_split_kind) c
    group by c.model_group_id;
$$;

-- Per group and date: own-row and common-row counts and errors. The common rows of a date are
-- the labeled rows every listed group scored; a date some group did not score has none.
create function triage.common_rows_metrics(
    p_model_group_ids bigint[],
    p_split_kind      triage.split_kind default 'test',
    p_as_of_date      date default null
)
returns table (model_group_id bigint, model_id bigint, as_of_date date,
               n_labeled bigint, n_scored_own bigint, n_common bigint,
               mae_own double precision, mae_common double precision,
               rmse_common double precision, wape_common double precision,
               bias_common double precision)
language plpgsql
stable
as $$
#variable_conflict use_column
declare
    v_groups bigint[] := array(select distinct g from unnest(p_model_group_ids) g order by g);
    v_dup    record;
begin
    select c.model_group_id, c.as_of_date, count(*) as n
      into v_dup
      from triage.scored_coverage(v_groups, p_split_kind) c
     where p_as_of_date is null or c.as_of_date = p_as_of_date
     group by c.model_group_id, c.as_of_date
    having count(*) > 1
     limit 1;
    if found then
        raise exception 'model group % has % models scoring % — compare one model per group'
            ' and date', v_dup.model_group_id, v_dup.n, v_dup.as_of_date;
    end if;

    return query
    with cov as materialized (
        select c.* from triage.scored_coverage(v_groups, p_split_kind) c
        where p_as_of_date is null or c.as_of_date = p_as_of_date
    ),
    own as materialized (
        select cov.model_group_id, cov.model_id, s.as_of_date, s.entity_id, s.score,
               s.outcome
        from   cov
        cross  join lateral triage.scored_labels(cov.model_id, p_split_kind) s
        where  s.as_of_date = cov.as_of_date and s.outcome is not null
    ),
    common as materialized (
        select own.as_of_date, own.entity_id
        from   own
        group  by own.as_of_date, own.entity_id
        having count(distinct own.model_group_id) = cardinality(v_groups)
    ),
    errors as (
        select own.model_id, own.as_of_date,
               count(*) as n_own,
               avg(abs(own.score - own.outcome)) as mae_own,
               count(c.entity_id) as n_common,
               avg(abs(own.score - own.outcome)) filter (where c.entity_id is not null)
                   as mae_common,
               sqrt(avg(power(own.score - own.outcome, 2))
                        filter (where c.entity_id is not null)) as rmse_common,
               sum(abs(own.score - own.outcome)) filter (where c.entity_id is not null)
                   / nullif(sum(abs(own.outcome)) filter (where c.entity_id is not null), 0)
                   as wape_common,
               avg(own.score - own.outcome) filter (where c.entity_id is not null)
                   as bias_common
        from   own
        left   join common c
               on c.as_of_date = own.as_of_date and c.entity_id = own.entity_id
        group  by own.model_id, own.as_of_date
    )
    select cov.model_group_id, cov.model_id, cov.as_of_date, cov.n_labeled,
           coalesce(e.n_own, 0), coalesce(e.n_common, 0),
           e.mae_own, e.mae_common, e.rmse_common, e.wape_common, e.bias_common
    from   cov
    left   join errors e
           on e.model_id = cov.model_id and e.as_of_date = cov.as_of_date
    order  by cov.as_of_date, cov.model_group_id;
end;
$$;

-- The common rows of the listed groups as a Subset; refilled on every call. Returns its hash.
create function triage.materialize_common_rows(
    p_model_group_ids bigint[],
    p_split_kind      triage.split_kind default 'test'
)
returns text
language plpgsql
as $$
declare
    v_groups bigint[] := array(select distinct g from unnest(p_model_group_ids) g order by g);
    v_hash   text := 'common:' || md5(array_to_string(v_groups, ',') || ':' || p_split_kind);
begin
    if cardinality(v_groups) < 2 then
        raise exception 'common rows need two or more model groups, got %', v_groups;
    end if;
    insert into triage.subsets (subset_hash, config)
    values (v_hash, jsonb_build_object(
                'name', 'rows scored by groups ' || array_to_string(v_groups, ', '),
                'common_rows', jsonb_build_object('model_group_ids', to_jsonb(v_groups),
                                                  'split_kind', p_split_kind::text)))
    on conflict (subset_hash) do nothing;

    delete from triage.subset_members where subset_hash = v_hash;
    insert into triage.subset_members (subset_hash, entity_id, as_of_date)
    with own as (
        select m.model_group_id, s.as_of_date, s.entity_id
        from   triage.models m
        cross  join lateral triage.scored_labels(m.model_id, p_split_kind) s
        where  m.model_group_id = any(v_groups) and s.outcome is not null
    )
    select v_hash, own.entity_id, own.as_of_date
    from   own
    group  by own.as_of_date, own.entity_id
    having count(distinct own.model_group_id) = cardinality(v_groups);
    return v_hash;
end;
$$;
"""

# ------------------------------------------------------------------- audition by subset

DISTANCES_BY_SUBSET_DDL = r"""
-- 0013's audition_distances keyed by subset: the best value, the distance from it and the next
-- split's distance are computed within one subset's evaluations.
create view triage.audition_distances_by_subset as
with base as (
    select r.experiment_hash, e.subset_hash, m.model_group_id, e.metric, e.parameter,
           e.as_of_date, m.train_end_time, e.value as raw_value,
           case when triage.higher_is_better(e.metric)
                then max(e.value) over w else min(e.value) over w end as best_value,
           abs((case when triage.higher_is_better(e.metric)
                     then max(e.value) over w else min(e.value) over w end) - e.value)
             as dist_from_best_case
    from   triage.evaluations e
    join   triage.models      m on m.model_id = e.model_id
    join   triage.runs        r on r.run_id   = m.run_id
    where  e.split_kind = 'test' and e.value is not null
    window w as (partition by r.experiment_hash, e.subset_hash, e.metric, e.parameter,
                              e.as_of_date)
)
select base.*,
       lead(dist_from_best_case) over (
           partition by experiment_hash, subset_hash, model_group_id, metric, parameter
           order by as_of_date
       ) as dist_from_best_case_next_time
from base;

-- The full-cohort rows, with 0013's columns: what every existing reader reads.
create or replace view triage.audition_distances as
select experiment_hash, model_group_id, metric, parameter, as_of_date, train_end_time,
       raw_value, best_value, dist_from_best_case, dist_from_best_case_next_time
from   triage.audition_distances_by_subset
where  subset_hash = '';
"""


def _windowed() -> str:
    text = _revision_ddl("0022_audition_window", "WINDOWED")
    text = _replace(
        text,
        "drop function if exists triage.audition_windowed(text, text, text, date, date);",
        "drop function triage.audition_windowed(text, text, text, date, date);",
        1,
    )
    text = _replace(
        text,
        "    p_from date default null, p_to date default null)",
        "    p_from date default null, p_to date default null, p_subset_hash text default '')",
        1,
    )
    text = _replace(
        text,
        "from   triage.audition_distances ad",
        "from   triage.audition_distances_by_subset ad",
        1,
    )
    return _replace(
        text,
        "where  ad.experiment_hash = p_experiment",
        "where  ad.subset_hash = p_subset_hash and ad.experiment_hash = p_experiment",
        1,
    )


def _pick() -> str:
    text = _revision_ddl("0022_audition_window", "PICK")
    text = _replace(
        text,
        "drop function if exists triage.audition_pick(text, text, text, text, jsonb, date, date);",
        "drop function if exists triage.audition_pick(text, text, text, text, jsonb, date, date);"
        "\ndrop function if exists triage.audition_pick("
        "text, text, text, text, jsonb, date, date, text);",
        1,
    )
    text = _replace(
        text,
        "    p_from date default null, p_to date default null)",
        "    p_from date default null, p_to date default null, p_subset_hash text default '')",
        1,
    )
    # every aggregation of a rule reads the subset's distances
    text = _replace(
        text,
        "from triage.audition_distances ad",
        "from triage.audition_distances_by_subset ad",
        5,
    )
    text = _replace(
        text,
        "where ad.experiment_hash = p_experiment",
        "where ad.subset_hash = p_subset_hash and ad.experiment_hash = p_experiment",
        5,
    )
    text = _replace(
        text,
        "(select max(as_of_date) from triage.audition_distances\n"
        "                               where experiment_hash = p_experiment",
        "(select max(as_of_date) from triage.audition_distances_by_subset\n"
        "                               where subset_hash = p_subset_hash\n"
        "                                 and experiment_hash = p_experiment",
        1,
    )
    return _replace(
        text,
        "triage.audition_windowed(p_experiment, p_metric, p_parameter, p_from, p_to)",
        "triage.audition_windowed(p_experiment, p_metric, p_parameter, p_from, p_to,"
        " p_subset_hash)",
        3,
    )


def _selected() -> str:
    text = _revision_ddl("0022_audition_window", "SELECTED")
    text = _replace(
        text,
        "drop function if exists triage.selected_model(text, text, text, text, date, date);",
        "drop function if exists triage.selected_model(text, text, text, text, date, date);"
        "\ndrop function if exists triage.selected_model("
        "text, text, text, text, date, date, text);",
        1,
    )
    text = _replace(
        text,
        "    p_from date default null, p_to date default null)",
        "    p_from date default null, p_to date default null, p_subset_hash text default '')",
        1,
    )
    return _replace(
        text,
        "'{}'::jsonb, p_from, p_to)",
        "'{}'::jsonb, p_from, p_to, p_subset_hash)",
        2,
    )


def upgrade() -> None:
    op.execute(COMMON_ROWS_DDL)
    op.execute(DISTANCES_BY_SUBSET_DDL)
    op.execute(_windowed())
    op.execute(_pick())
    op.execute(_selected())


def downgrade() -> None:
    op.execute(
        "drop function triage.selected_model(text, text, text, text, date, date, text);"
        " drop function triage.audition_pick(text, text, text, text, jsonb, date, date, text);"
        " drop function triage.audition_windowed(text, text, text, date, date, text);"
    )
    op.execute(_revision_ddl("0022_audition_window", "WINDOWED"))
    op.execute(_revision_ddl("0022_audition_window", "PICK"))
    op.execute(_revision_ddl("0022_audition_window", "SELECTED"))
    # 0013's audition_distances, then the subset view it no longer reads
    op.execute(
        "create or replace view triage.audition_distances as"
        " with base as ("
        "   select r.experiment_hash, m.model_group_id, e.metric, e.parameter, e.as_of_date,"
        "          m.train_end_time, e.value as raw_value,"
        "          case when triage.higher_is_better(e.metric)"
        "               then max(e.value) over w else min(e.value) over w end as best_value,"
        "          abs((case when triage.higher_is_better(e.metric)"
        "                    then max(e.value) over w else min(e.value) over w end) - e.value)"
        "            as dist_from_best_case"
        "   from triage.evaluations e"
        "   join triage.models m on m.model_id = e.model_id"
        "   join triage.runs r on r.run_id = m.run_id"
        "   where e.split_kind = 'test' and e.value is not null and e.subset_hash = ''"
        "   window w as (partition by r.experiment_hash, e.metric, e.parameter, e.as_of_date))"
        " select base.*, lead(dist_from_best_case) over ("
        "     partition by experiment_hash, model_group_id, metric, parameter"
        "     order by as_of_date) as dist_from_best_case_next_time"
        " from base"
    )
    op.execute("drop view triage.audition_distances_by_subset")
    op.execute(
        "drop function triage.materialize_common_rows(bigint[], triage.split_kind);"
        " drop function triage.common_rows_metrics(bigint[], triage.split_kind, date);"
        " drop function triage.model_group_coverage(text, triage.split_kind);"
        " drop function triage.scored_coverage(bigint[], triage.split_kind);"
        " drop function triage.label_source(bigint, triage.split_kind);"
    )
