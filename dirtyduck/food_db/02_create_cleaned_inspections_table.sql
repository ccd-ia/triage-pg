-- Clean layer (DB-audit #2: renamed `cleaned` -> `clean` to match the raw/clean/ontology convention).
create schema if not exists clean;

-- Stable, low-cardinality categoricals as ENUMs (DB-audit #3): enforce the domain AND give
-- featurizer a fixed one-hot vocabulary for free (adapter-spec §4). High-cardinality columns
-- stay text: facility_type (287 distinct) + zip_code — the adapter's train-fit encoder handles
-- them; a lookup/dimension table is the documented escape if a domain churns.
drop type if exists clean.result_t cascade;
create type clean.result_t as enum
  ('pass', 'pass w/ conditions', 'fail', 'no entry', 'not ready', 'out of business', 'business not located');

drop type if exists clean.risk_t cascade;
create type clean.risk_t as enum ('low', 'medium', 'high');

drop type if exists clean.inspection_type_t cascade;
-- 'task force' is included defensively: the substring pattern can emit it (liquor->task force),
-- even though no current row does — an unlisted value would fail the cast.
create type clean.inspection_type_t as enum
  ('canvass', 'task force', 'complaint', 'food poisoning', 'consultation', 'license', 'tag removal');

-- severity is assigned in 03 (clean.violations); the type is defined here so it exists first.
drop type if exists clean.severity_t cascade;
create type clean.severity_t as enum ('minor', 'serious', 'critical');

drop table if exists clean.inspections cascade;

create table clean.inspections as (
  with cleaned as (
    select
      inspection::integer,
      btrim(lower(results))::clean.result_t as result,
      license_num::integer,
      btrim(lower(dba_name)) as facility,
      btrim(lower(aka_name)) as facility_aka,
      case when
           facility_type is null then 'unknown'
      else btrim(lower(facility_type))
      end as facility_type,
      lower(substring(risk from '\((.+)\)'))::clean.risk_t as risk,
      btrim(lower(address)) as address,
      zip as zip_code,
      substring(
        btrim(lower(regexp_replace(type, 'liquor', 'task force', 'gi')))
        from 'canvass|task force|complaint|food poisoning|consultation|license|tag removal')::clean.inspection_type_t as type,
      date,
        -- point(longitude, latitude) as location
      ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography as location  -- We use geography so the measurements are in meters
      from raw.inspections
     where zip is not null  -- removing NULL zip codes
  )

  select * from cleaned where type is not null
);
