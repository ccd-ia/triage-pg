-- DonorsChoose (KDD Cup 2014) — ONTOLOGY layer (entity-state-event for featurizer + triage).
-- entity = a posted PROJECT (target). The static attributes are point-in-time features; the
-- teacher/school keys let the FEATURIZER config synthesize prior-project history via a
-- self-referential as-of join (ADR-0008 — featurizer does the feature engineering, we just
-- expose the graph). events = the project's RESOURCES (line items known at posting → a clean,
-- leakage-free child stream featurizer aggregates). DONATIONS are NOT here: they are the label
-- source only (zero at posting + using them as features would leak).

create schema if not exists ontology;

-- ----------------------------------------------------------------- ontology.entities = projects
-- entity_id is a stable surrogate ordered by posting time. date_posted is the knowledge date
-- (the decision point). teacher_acctid / schoolid drive featurizer's prior-history relationships.
drop table if exists ontology.entities cascade;
create table ontology.entities as
select
    row_number() over (order by date_posted, projectid) as entity_id,
    projectid,
    teacher_acctid,
    schoolid,
    school_state,
    school_metro,
    school_charter,
    school_magnet,
    teacher_prefix,
    teacher_teach_for_america,
    primary_focus_area,
    primary_focus_subject,
    resource_type,
    poverty_level,
    grade_level,
    eligible_double_match,
    total_price,
    students_reached,
    date_posted
from clean.projects;

alter table ontology.entities add primary key (entity_id);
create unique index entities_project_uix on ontology.entities (projectid);
create index entities_teacher_ix on ontology.entities (teacher_acctid);
create index entities_school_ix on ontology.entities (schoolid);
create index entities_posted_ix on ontology.entities (date_posted);

-- ----------------------------------------------------------------- ontology.events = resources
-- One event per requested resource line item, keyed to the project's entity_id. The knowledge
-- date is the project's date_posted (resources are declared at posting), so featurizer's as-of
-- aggregation is point-in-time-correct. featurizer synthesizes count / sum·avg unit price /
-- resource-type mix from this stream.
drop table if exists ontology.events cascade;
create table ontology.events as
select
    row_number() over (order by e.date_posted, r.resourceid) as event_id,
    e.entity_id,
    e.date_posted                                     as date,   -- knowledge date = posting
    r.project_resource_type,
    r.item_unit_price,
    r.item_quantity,
    (coalesce(r.item_unit_price, 0) * coalesce(r.item_quantity, 0)) as line_total,
    r.vendorid
from clean.resources r
join ontology.entities e using (projectid);

create index events_entity_ix on ontology.events (entity_id);
create index events_date_ix on ontology.events (date);
create index events_entity_date_ix on ontology.events (entity_id, date);
create index events_rtype_ix on ontology.events (project_resource_type);

-- ----------------------------------------------------------------- convenience: realized funding
-- The 4-month-funded OUTCOME per project (teaching / sanity / EDA). The triage label query
-- computes the SAME thing point-in-time over the [as_of, as_of + horizon) window; this view is
-- the whole-history realized value. funded_4mo = donations within 4 months ≥ total_price.
drop view if exists ontology.project_funding cascade;
create view ontology.project_funding as
select
    e.entity_id,
    e.projectid,
    e.date_posted,
    e.total_price,
    coalesce(sum(d.donation_to_project), 0)           as raised_total,
    coalesce(sum(d.donation_to_project) filter (
        where d.donation_timestamp < e.date_posted + interval '4 months'), 0) as raised_4mo,
    (coalesce(sum(d.donation_to_project) filter (
        where d.donation_timestamp < e.date_posted + interval '4 months'), 0)
        < e.total_price) as unfunded_4mo   -- TRUE = the EWS positive (needs help)
from ontology.entities e
left join clean.donations d on d.projectid = e.projectid
group by e.entity_id, e.projectid, e.date_posted, e.total_price;
