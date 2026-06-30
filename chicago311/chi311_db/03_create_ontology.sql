-- Chicago 311 Service Requests — ONTOLOGY layer (entity-state-event for featurizer + triage).
--
-- entity = a filed SERVICE REQUEST (the target). Its static attributes (type, department, origin,
-- location) are point-in-time features known AT FILING = the decision point. The community_area /
-- sr_type keys let the FEATURIZER config aggregate the request's geographic + type *backlog* via
-- self-referential as-of joins (ADR-0008 — featurizer does the feature engineering; we expose the
-- graph). events = the same filings re-projected as a location/type EVENT stream that those joins
-- aggregate (a leakage-free child: it carries only what is known at filing — where, what, when —
-- never the resolution). closed_date is the LABEL source only (resolution is the future target).
--
-- The ML problem (early-warning system): for a just-filed request, will it take LONGER than the
-- SLA (default 14 days) to resolve? outcome=1 ("slow") is the request an operator would escalate.

create schema if not exists ontology;

-- ----------------------------------------------------------------- ontology.entities = requests
-- entity_id is a stable surrogate ordered by filing time. created_date is the knowledge date (the
-- decision point). community_area / sr_type drive featurizer's backlog relationships.
drop table if exists ontology.entities cascade;
create table ontology.entities as
select
    row_number() over (order by created_date, sr_number) as entity_id,
    sr_number,
    sr_type,
    sr_short_code,   -- 1:1 with sr_type; the dedicated type_demand join key (kept distinct from
                     -- the sr_type FEATURE so featurizer never sees a key that doubles as a column)
    owner_department,
    origin,
    community_area,
    ward,
    police_district,
    zip_code,
    latitude,
    longitude,
    created_day_of_week,
    created_hour,
    created_month,
    created_date,
    closed_date,
    status
from clean.requests;

alter table ontology.entities add primary key (entity_id);
create unique index entities_sr_uix on ontology.entities (sr_number);
create index entities_area_ix on ontology.entities (community_area);
create index entities_type_ix on ontology.entities (sr_type);
create index entities_created_ix on ontology.entities (created_date);

-- ----------------------------------------------------------------- ontology.events = filing events
-- One row per filed request, carrying ONLY filing-time facts (location, type, when). featurizer
-- aggregates this stream as-of: COUNT over the same community_area = the area's recent backlog;
-- COUNT over the same sr_type = recent demand for that service. The knowledge date is created_date,
-- so the as-of aggregation is point-in-time-correct. Resolution is deliberately absent (it would leak).
drop table if exists ontology.events cascade;
create table ontology.events as
select
    e.entity_id                          as event_id,
    e.entity_id,
    e.community_area,
    e.sr_type,            -- area_backlog's measure variable (type-mix within the neighbourhood)
    e.sr_short_code,      -- type_demand's join key (1:1 with sr_type; never declared as a feature)
    e.created_date                       as date   -- knowledge date = filing
from ontology.entities e;

create index events_entity_ix on ontology.events (entity_id);
create index events_area_ix on ontology.events (community_area);
create index events_code_ix on ontology.events (sr_short_code);
create index events_date_ix on ontology.events (date);
create index events_area_date_ix on ontology.events (community_area, date);
create index events_code_date_ix on ontology.events (sr_short_code, date);

-- ----------------------------------------------------------------- convenience: realized resolution
-- days_to_close + the realized SLA breach (slow = NOT resolved within 14 days), for EDA / teaching /
-- sanity. The triage label query computes the SAME thing point-in-time over the label window; this
-- view is the whole-history realized value. resolved within 14d ⇒ NOT slow (the EWS negative).
drop view if exists ontology.request_resolution cascade;
create view ontology.request_resolution as
select
    e.entity_id,
    e.sr_number,
    e.created_date,
    e.closed_date,
    e.status,
    case when e.closed_date is not null
         then round(extract(epoch from (e.closed_date - e.created_date)) / 86400.0, 2)
    end                                                            as days_to_close,
    (e.closed_date is null
     or e.closed_date >= e.created_date + interval '14 days')      as slow_14d   -- TRUE = EWS positive
from ontology.entities e;
