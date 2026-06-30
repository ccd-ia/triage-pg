-- Chicago 311 Service Requests — CLEAN layer (pg-data-discovery methodology).
-- raw (all text) → clean (typed). Nulls are PRESERVED (no imputation — that is a modeling
-- decision for the triage adapter, ADR-0009). Timestamps → timestamp, numeric codes → int /
-- numeric, 't'/'f' checkboxes → boolean. We keep the columns the ontology needs and drop the
-- ingestion-only noise; the faithful representation lives here, the entity-state-event shaping
-- happens in 03_create_ontology.sql.

create schema if not exists clean;

drop table if exists clean.requests cascade;
create table clean.requests as
select
    sr_number,
    nullif(sr_type, '')                          as sr_type,
    nullif(sr_short_code, '')                    as sr_short_code,
    nullif(owner_department, '')                 as owner_department,
    nullif(status, '')                           as status,
    nullif(origin, '')                           as origin,
    nullif(created_date, '')::timestamp          as created_date,
    nullif(closed_date, '')::timestamp           as closed_date,
    nullif(last_modified_date, '')::timestamp    as last_modified_date,
    nullif(street_address, '')                   as street_address,
    nullif(zip_code, '')                         as zip_code,
    nullif(community_area, '')::integer          as community_area,
    nullif(ward, '')::integer                    as ward,
    nullif(police_district, '')                  as police_district,
    nullif(latitude, '')::double precision       as latitude,
    nullif(longitude, '')::double precision      as longitude,
    nullif(created_day_of_week, '')::integer     as created_day_of_week,
    nullif(created_hour, '')::integer            as created_hour,
    nullif(created_month, '')::integer           as created_month,
    nullif(duplicate, '')::boolean               as duplicate,
    nullif(legacy_record, '')::boolean           as legacy_record
from raw.requests
where created_date ~ '^\d{4}-\d{2}-\d{2}';

alter table clean.requests add primary key (sr_number);
create index clean_requests_created_ix on clean.requests (created_date);
create index clean_requests_area_ix on clean.requests (community_area);
create index clean_requests_type_ix on clean.requests (sr_type);
