"""feature-importance betas/odds + per-type source volume

Revision ID: 0009_betas_typevolume
Revises: 0008_entity_geo_labels
Create Date: 2026-06-26

Two additions:

* **feature-importance semantics** — ``feature_importances`` only stored ``feature_importance``
  (``|coef|`` for linear models, Gini for trees), which is ambiguous and, for linear models,
  scale-dependent. Add ``importance_kind`` (``'gini'`` | ``'abs_coef'`` | ``'coef'``), the
  signed coefficient ``signed_value`` (β), and ``odds_ratio`` (exp β) so the dashboard can show
  betas/odds for logistic models. Tree models leave the linear columns NULL.

* **per-type source volume** — the Ontology view showed one volume-over-time series per source.
  Add a nullable ``triage.sources.type_column`` (the categorical column that types the rows —
  e.g. facility_type for entities, inspection type for events) and ``source_volume_by_type`` so
  the view can break volume out by entity/event type. Degrades to nothing when ``type_column``
  is unset.
"""

from alembic import op

# revision identifiers, used by Alembic. (id <= 32 chars: results_schema_versions.version_num)
revision = "0009_betas_typevolume"
down_revision = "0008_entity_geo_labels"
branch_labels = None
depends_on = None


UPGRADE_DDL = r"""
-- ------------------------------------------------------------- feature-importance semantics
alter table triage.feature_importances add column if not exists importance_kind text;
alter table triage.feature_importances add column if not exists signed_value double precision;
alter table triage.feature_importances add column if not exists odds_ratio double precision;

-- ------------------------------------------------------------- per-type source volume
alter table triage.sources add column if not exists type_column text;

create or replace function triage.source_volume_by_type(p_source text, p_grain text default 'month')
returns table(period date, type_value text, n bigint)
language plpgsql stable as $fn$
declare rel text; kd text; tc text;
begin
    select s.relation, s.knowledge_date_column, s.type_column into rel, kd, tc
    from triage.sources s where s.source_name = p_source;
    if rel is null or tc is null then return; end if;
    if kd is null then
        return query execute format(
            'select null::date as period, %I::text as type_value, count(*)::bigint as n'
            || ' from %s group by 2 order by 2', tc, rel::regclass::text);
    else
        return query execute format(
            'select date_trunc(%L, %I)::date as period, %I::text as type_value, count(*)::bigint as n'
            || ' from %s group by 1, 2 order by 1, 2', p_grain, kd, tc, rel::regclass::text);
    end if;
end;
$fn$;
"""


DOWNGRADE_DDL = r"""
drop function if exists triage.source_volume_by_type(text, text);
alter table triage.sources drop column if exists type_column;
alter table triage.feature_importances drop column if exists odds_ratio;
alter table triage.feature_importances drop column if exists signed_value;
alter table triage.feature_importances drop column if exists importance_kind;
"""


def upgrade():
    op.execute(UPGRADE_DDL)


def downgrade():
    op.execute(DOWNGRADE_DDL)
