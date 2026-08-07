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

-- Events carry ONLY event-specific columns (type / date / risk / result / violations). Entity
-- attributes (facility_type / zip_code / location) live on ontology.entities and are NOT copied
-- here (Option A, 2026-07-06): facility_type + address are part of the entity DISTINCT ON identity,
-- so they are constant within an entity and were provably redundant on events (0 within-entity
-- variation over 18,909 entities; 0/74,191 event↔entity mismatch). A feature needing them joins
-- ontology.entities on entity_id — the value is constant, so the join is exact and leakage-free.
create table ontology.events as (

        with entities as (
        select * from ontology.entities
            ),

        inspections as (
        select
            i.inspection, i.type, i.date, i.risk, i.result,
            i.license_num, i.facility, i.facility_aka,
            i.facility_type, i.address,
            -- Typed promotions of the violations content (the ontology promotion rule,
            -- CONTEXT.md "Event"): promote what gets featurized, keep the jsonb as the
            -- retained long tail. severity is a DETERMINISTIC function of code
            -- (03_create_violations_table.sql: 1-14 critical / 15-29 serious / else minor)
            -- and description is the code's canonical text (1:1) — so counting by severity
            -- captures everything free-text description mining could. An inspection with
            -- no violations carries one placeholder row with code = '' — nullif excludes it.
            count(*) filter (where nullif(v.code, '') is not null)  as n_violations,
            count(*) filter (where v.severity = 'critical')         as n_critical,
            count(*) filter (where v.severity = 'serious')          as n_serious,
            count(*) filter (where v.severity = 'minor')            as n_minor,
            -- Keyword flags over the inspector's free-text comment (the only genuinely
            -- free text — see above). Plain PostgreSQL regex, word-bounded (\y) so 'rat'
            -- cannot match 'refrigeration'. The list is deliberately SHORT: each term is a
            -- concept a health inspector actually writes down; a longer list overfits this
            -- dataset and turns a teaching feature into a leaderboard trick.
            max((v.comment ~ '\y(rodent|rodents|mice|mouse|rat|rats|droppings)\y')::int) as kw_rodent,
            max((v.comment ~ '\y(roach|roaches|cockroach|cockroaches|insect|insects|flies)\y')::int) as kw_insect,
            max((v.comment ~ 'temperature|thermometer')::int)       as kw_temperature,
            max((v.comment ~ '\y(handwashing|hand washing|hand sink|soap)\y')::int) as kw_handwashing,
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
            i.facility_aka, i.facility_type, i.address,
            i.date, i.risk, i.result
            )

    select
        i.inspection as event_id,
        e.entity_id, i.type, i.date, i.risk, i.result,
        i.n_violations, i.n_critical, i.n_serious, i.n_minor,
        i.kw_rodent, i.kw_insect, i.kw_temperature, i.kw_handwashing,
        i.violations
    from
        entities as e
        inner join
        inspections as i
        using (license_num, facility, facility_aka, facility_type, address)
        );

-- Add some indices
create index events_entity_ix on ontology.events (entity_id asc nulls last);
create index events_event_ix on ontology.events (event_id asc nulls last);
create index events_type_ix on ontology.events (type);
create index events_date_ix on ontology.events(date asc nulls last);

-- JSONB indices
create index events_violations on ontology.events using gin(violations);
create index events_violations_json_path on ontology.events using gin(violations jsonb_path_ops);

create index events_event_entity_date on ontology.events (event_id asc nulls last, entity_id asc nulls last, date desc nulls last);
