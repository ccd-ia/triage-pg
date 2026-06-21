-- Ontology layer (DB-audit #2: renamed `semantic` -> `ontology`, the entity-state-event layer).
-- result / risk / type propagate as their clean.*_t ENUMs; facility_type / zip_code stay text.
create schema if not exists ontology;

drop table if exists ontology.entities cascade;

create table ontology.entities as (
        with entities as (
        select
            distinct on (
                license_num,
                facility,
                facility_aka,
                facility_type,
                address
                )
            license_num,
            facility,
            facility_aka,
            facility_type,
            address,
            zip_code,
            location,
            min(date) over (partition by license_num, facility, facility_aka, facility_type, address) as start_time,
            max(case when result in ('out of business', 'business not located')
                then date
                else NULL
                end)
            over (partition by license_num, facility, facility_aka, address) as end_time
        from clean.inspections
        order by
            license_num, facility, facility_aka, facility_type, address,
            date asc -- IMPORTANT!!
            )

    select
        row_number() over (order by start_time asc ) as entity_id,
        license_num,
        facility,
        facility_aka,
        facility_type,
        address,
        zip_code,
        location,
        start_time,
        end_time,
        daterange(start_time, end_time) as activity_period
    from entities
        );

create index entities_ix on ontology.entities (entity_id);
create index entities_license_num_ix on ontology.entities (license_num);
create index entities_facility_ix on ontology.entities (facility);
create index entities_facility_type_ix on ontology.entities (facility_type);
create index entities_zip_code_ix on ontology.entities (zip_code);

-- Spatial index
create index entities_location_gix on ontology.entities using gist (location);

-- Temporal index on the activity_period daterange — backs point-in-time cohort selection
-- (`activity_period @> as_of_date`, DB-audit #6) without rebuilding the range per query.
create index entities_activity_gix on ontology.entities using gist (activity_period);

create index entities_full_key_ix on ontology.entities (license_num, facility, facility_aka, facility_type, address);

drop table if exists ontology.events cascade;

-- NOTE (DB-audit #5): events carry facility_type / zip_code / location — the entity's attributes
-- *at event time*. This is deliberate point-in-time state capture (the "state" of entity-state-
-- event): a facility's type/zip can change, and an inspection records the value when it happened,
-- which is more point-in-time-correct than joining the entity's current value. Not redundancy.
create table ontology.events as (

        with entities as (
        select * from ontology.entities
            ),

        inspections as (
        select
            i.inspection, i.type, i.date, i.risk, i.result,
            i.license_num, i.facility, i.facility_aka,
            i.facility_type, i.address, i.zip_code, i.location,
            jsonb_agg(
                jsonb_build_object(
                    'code', v.code,
                    'severity', v.severity,
	                'description', v.description,
	                'comment', v.comment
	                )
            order  by code
                ) as violations
        from
            clean.inspections as i
            inner join
            clean.violations as v
            on i.inspection = v.inspection
        group by
            i.inspection, i.type, i.license_num, i.facility,
            i.facility_aka, i.facility_type, i.address, i.zip_code, i.location,
            i.date, i.risk, i.result
            )

    select
        i.inspection as event_id,
        e.entity_id, i.type, i.date, i.risk, i.result,
        e.facility_type, e.zip_code, e.location,
        i.violations
    from
        entities as e
        inner join
        inspections as i
        using (license_num, facility, facility_aka, facility_type, address, zip_code)
        );

-- Add some indices
create index events_entity_ix on ontology.events (entity_id asc nulls last);
create index events_event_ix on ontology.events (event_id asc nulls last);
create index events_type_ix on ontology.events (type);
create index events_date_ix on ontology.events(date asc nulls last);
create index events_facility_type_ix on ontology.events  (facility_type);
create index events_zip_code_ix on ontology.events  (zip_code);

-- Spatial index
create index events_location_gix on ontology.events using gist (location);

-- JSONB indices
create index events_violations on ontology.events using gin(violations);
create index events_violations_json_path on ontology.events using gin(violations jsonb_path_ops);

create index events_event_entity_zip_code_date on ontology.events (event_id asc nulls last, entity_id asc nulls last, zip_code, date desc nulls last);
