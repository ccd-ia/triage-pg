"""entity attributes come from the model's Experiment, keyed by its target's id (#88)

``entity_attributes(entity)`` (0008) took ``role = 'entity'`` from ``triage.sources`` with
``limit 1`` and no order, so in a database holding several Experiments it could read
another Experiment's entity relation. It then filtered on ``t.entity_id``, so an entity
relation keyed by another column (featurizer's target ``id``, such as ``pair_id``)
raised ``column t.entity_id does not exist``, and ``triage postmodel residuals`` failed
while printing its largest errors.

* ``entity_row(relation, key, knowledge_date, entity)`` reads one entity's row as jsonb,
  with 0008's geography columns as points. It returns NULL when the relation or the key
  column does not exist, and ignores a knowledge-date column that does not exist.
* ``entity_attributes(entity, model)`` resolves the source from the model's Experiment:
  first an entity source a Run of the Experiment pinned (``run_source_pins``), then the
  table of the target entity in the latest Run's ``feature_config``. The key is the
  target's ``id``, else ``entity_id``. NULL when neither gives a usable relation.
* ``largest_errors`` calls it with its model.
* ``entity_attributes(entity)`` keeps its signature for the dashboard's entity page. It
  now takes the entity sources in ``source_name`` order and skips one without an
  ``entity_id`` column instead of raising.

Downgrade restores 0008's and 0033's text and drops the two new functions.

Revision ID: 0041_entity_attributes_scoped
Revises: 0040_ndcg_at_k
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "0041_entity_attributes_scoped"
down_revision = "0040_ndcg_at_k"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- One entity's row of a relation as jsonb, geography/geometry columns as {lon, lat,
-- geojson, kind} (0008's rule). NULL when the relation or the key column does not exist.
create function triage.entity_row(
    p_relation       text,
    p_key            text,
    p_knowledge_date text,
    p_entity         bigint
)
returns jsonb
language plpgsql
stable
as $$
declare
    rel          regclass := to_regclass(p_relation);
    kd           text := p_knowledge_date;
    result       jsonb;
    geo_override text := '';
    grec         record;
begin
    if rel is null or p_key is null then
        return null;
    end if;
    if not exists (select 1 from pg_attribute a
                    where a.attrelid = rel and a.attname = p_key
                      and a.attnum > 0 and not a.attisdropped) then
        return null;
    end if;
    if kd is not null and not exists (select 1 from pg_attribute a
                                       where a.attrelid = rel and a.attname = kd
                                         and a.attnum > 0 and not a.attisdropped) then
        kd := null;
    end if;

    for grec in
        select a.attname as col
        from   pg_attribute a
        join   pg_type ty on ty.oid = a.atttypid
        where  a.attrelid = rel and a.attnum > 0 and not a.attisdropped
          and  ty.typname in ('geography', 'geometry')
    loop
        geo_override := geo_override || format(
            ' || jsonb_build_object(%L, case when t.%I is null then null else jsonb_build_object('
            || '''lon'', ST_X(ST_Centroid(t.%I::geometry)),'
            || '''lat'', ST_Y(ST_Centroid(t.%I::geometry)),'
            || '''geojson'', ST_AsGeoJSON(t.%I)::jsonb,'
            || '''kind'', ''geo'') end)',
            grec.col, grec.col, grec.col, grec.col, grec.col);
    end loop;

    execute format(
        'select to_jsonb(t) %s from %s t where t.%I = $1 %s limit 1',
        geo_override,
        rel::text,
        p_key,
        case when kd is null then '' else format('order by %I desc', kd) end
    ) into result using p_entity;
    return result;
end;
$$;

-- The attributes of an entity as the model's Experiment declares it: its pinned entity
-- source, else its target entity's table, keyed by the target's id (#88).
create function triage.entity_attributes(p_entity bigint, p_model bigint)
returns jsonb
language plpgsql
stable
as $$
declare
    exp    text;
    fc     jsonb;
    target jsonb;
    rel    text;
    kd     text;
    key    text;
begin
    select r.experiment_hash into exp
    from   triage.models m
    join   triage.runs r on r.run_id = m.run_id
    where  m.model_id = p_model;
    if exp is null then
        return null;
    end if;

    -- the latest Run of the Experiment that recorded a featurizer config with a target
    select r.plan -> 'attempt' -> 'feature_config' into fc
    from   triage.runs r
    where  r.experiment_hash = exp
      and  r.plan -> 'attempt' -> 'feature_config' ? 'target'
    order  by r.started_at desc
    limit  1;
    select e into target
    from   jsonb_array_elements(coalesce(fc -> 'entities', '[]'::jsonb)) e
    where  e ->> 'alias' = fc ->> 'target'
    limit  1;

    select s.relation, s.knowledge_date_column into rel, kd
    from   triage.sources s
    join   triage.run_source_pins p on p.source_name = s.source_name
    join   triage.runs r on r.run_id = p.run_id
    where  r.experiment_hash = exp and s.role = 'entity'
    order  by r.started_at desc, s.source_name
    limit  1;
    if rel is null then
        rel := target ->> 'table';
        kd := target ->> 'temporal_ix';
    end if;

    -- the key: the target's id when the relation has that column, else entity_id
    key := target ->> 'id';
    if key is null or not exists (select 1 from pg_attribute a
                                   where a.attrelid = to_regclass(rel) and a.attname = key
                                     and a.attnum > 0 and not a.attisdropped) then
        key := 'entity_id';
    end if;
    return triage.entity_row(rel, key, kd, p_entity);
end;
$$;

-- Without a model: the entity sources in name order, the first with an entity_id column,
-- else 0008's fallback, the smallest registered relation that has one.
create or replace function triage.entity_attributes(p_entity bigint)
returns jsonb
language plpgsql
stable
as $$
declare
    rel text;
    kd  text;
begin
    select s.relation, s.knowledge_date_column into rel, kd
    from   triage.sources s
    where  s.role = 'entity'
      and  exists (select 1 from pg_attribute a
                    where a.attrelid = to_regclass(s.relation) and a.attname = 'entity_id'
                      and a.attnum > 0 and not a.attisdropped)
    order  by s.source_name
    limit  1;

    if rel is null then
        select s.relation, s.knowledge_date_column into rel, kd
        from   triage.sources s
        where  exists (select 1 from pg_attribute a
                        where a.attrelid = to_regclass(s.relation) and a.attname = 'entity_id'
                          and a.attnum > 0 and not a.attisdropped)
        order  by (select c.reltuples from pg_class c
                    where c.oid = to_regclass(s.relation)) asc, s.source_name
        limit  1;
    end if;

    return triage.entity_row(rel, 'entity_id', kd, p_entity);
end;
$$;
"""


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


def _block(module: str, constant: str, start: str, end: str = "\n$$;\n") -> str:
    """The one function of ``module.constant`` from ``start`` through the first ``end``."""
    text = getattr(_revision_module(module), constant)
    if text.count(start) != 1:
        raise RuntimeError(
            f"{start!r} occurs {text.count(start)} times in {module}.{constant}"
        )
    i = text.index(start)
    return text[i : text.index(end, i) + len(end)]


def _largest_errors_0033() -> str:
    block = _block(
        "0033_regression_residuals",
        "UPGRADE_DDL",
        "create function triage.largest_errors(",
    )
    return _replace_once(
        block,
        "create function triage.largest_errors(",
        "create or replace function triage.largest_errors(",
    )


def _largest_errors_0041() -> str:
    return _replace_once(
        _largest_errors_0033(),
        "triage.entity_attributes(top.entity_id)",
        "triage.entity_attributes(top.entity_id, p_model)",
    )


def _entity_attributes_0008() -> str:
    return _block(
        "0008_entity_geo_labels",
        "UPGRADE_DDL",
        "create or replace function triage.entity_attributes(p_entity bigint)",
        "\n$fn$;\n",
    )


def upgrade() -> None:
    op.execute(UPGRADE_DDL)
    op.execute(_largest_errors_0041())


def downgrade() -> None:
    op.execute(_largest_errors_0033())
    op.execute(_entity_attributes_0008())
    op.execute(
        "drop function triage.entity_attributes(bigint, bigint);"
        " drop function triage.entity_row(text, text, text, bigint);"
    )
