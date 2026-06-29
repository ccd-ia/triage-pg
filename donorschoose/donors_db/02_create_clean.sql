-- DonorsChoose (KDD Cup 2014) — CLEAN layer (pg-data-discovery methodology).
-- raw (all text) → clean (typed, snake_case already). Nulls are PRESERVED (no imputation —
-- that's a modeling decision for the triage adapter, ADR-0009). 't'/'f' → boolean, money →
-- numeric, dates → date/timestamp. The clean layer represents the data faithfully; the
-- ontology layer (03) shapes it into the entity-state-event graph featurizer consumes.

create schema if not exists clean;

-- ----------------------------------------------------------------- clean.projects
drop table if exists clean.projects cascade;
create table clean.projects as
select
    projectid,
    teacher_acctid,
    schoolid,
    nullif(school_state, '')                         as school_state,
    nullif(school_metro, '')                         as school_metro,
    nullif(school_city, '')                          as school_city,
    nullif(school_zip, '')                           as school_zip,
    nullif(school_latitude, '')::double precision    as school_latitude,
    nullif(school_longitude, '')::double precision   as school_longitude,
    nullif(school_charter, '')::boolean              as school_charter,
    nullif(school_magnet, '')::boolean               as school_magnet,
    nullif(teacher_prefix, '')                       as teacher_prefix,
    nullif(teacher_teach_for_america, '')::boolean   as teacher_teach_for_america,
    nullif(primary_focus_area, '')                   as primary_focus_area,
    nullif(primary_focus_subject, '')                as primary_focus_subject,
    nullif(secondary_focus_area, '')                 as secondary_focus_area,
    nullif(resource_type, '')                        as resource_type,
    nullif(poverty_level, '')                        as poverty_level,
    nullif(grade_level, '')                          as grade_level,
    nullif(eligible_double_your_impact_match, '')::boolean as eligible_double_match,
    nullif(total_price_excluding_optional_support, '')::numeric as total_price,
    nullif(total_price_including_optional_support, '')::numeric as total_price_incl_optional,
    nullif(students_reached, '')::integer            as students_reached,
    nullif(date_posted, '')::date                    as date_posted
from raw.projects
where date_posted ~ '^\d{4}-\d{2}-\d{2}$';

alter table clean.projects add primary key (projectid);
create index clean_projects_teacher_ix on clean.projects (teacher_acctid);
create index clean_projects_school_ix on clean.projects (schoolid);
create index clean_projects_posted_ix on clean.projects (date_posted);

-- ----------------------------------------------------------------- clean.donations
-- The LABEL source. Keep amount + timestamp; the funded-within-N-months target is computed
-- point-in-time in the triage label query (NOT materialized here).
drop table if exists clean.donations cascade;
create table clean.donations as
select
    donationid,
    projectid,
    donor_acctid,
    nullif(donation_timestamp, '')::timestamp        as donation_timestamp,
    nullif(donation_to_project, '')::numeric         as donation_to_project,
    nullif(donation_total, '')::numeric              as donation_total,
    nullif(is_teacher_acct, '')::boolean             as is_teacher_acct,
    nullif(payment_method, '')                       as payment_method
from raw.donations
where donation_timestamp ~ '^\d{4}-\d{2}-\d{2}';

create index clean_donations_project_ix on clean.donations (projectid);
create index clean_donations_ts_ix on clean.donations (donation_timestamp);
create index clean_donations_project_ts_ix on clean.donations (projectid, donation_timestamp);

-- ----------------------------------------------------------------- clean.resources
-- A featurizer child stream (line items requested at posting → point-in-time-safe features).
drop table if exists clean.resources cascade;
create table clean.resources as
select
    resourceid,
    projectid,
    nullif(vendorid, '')                             as vendorid,
    nullif(vendor_name, '')                          as vendor_name,
    nullif(project_resource_type, '')                as project_resource_type,
    nullif(item_name, '')                            as item_name,
    nullif(item_unit_price, '')::numeric             as item_unit_price,
    nullif(item_quantity, '')::numeric               as item_quantity
from raw.resources;

create index clean_resources_project_ix on clean.resources (projectid);

-- ----------------------------------------------------------------- clean.outcomes
-- The official KDD labels (funded-EVER etc.). Carried for reference/teaching; the triage target
-- is the 4-month-derived label (ADR-0010 decision). Present only for projects posted < 2014.
drop table if exists clean.outcomes cascade;
create table clean.outcomes as
select
    projectid,
    nullif(is_exciting, '')::boolean                 as is_exciting,
    nullif(fully_funded, '')::boolean                as fully_funded,
    nullif(at_least_1_teacher_referred_donor, '')::boolean as at_least_1_teacher_referred_donor,
    nullif(great_chat, '')::boolean                  as great_chat,
    nullif(teacher_referred_count, '')::numeric      as teacher_referred_count,
    nullif(non_teacher_referred_count, '')::numeric  as non_teacher_referred_count
from raw.outcomes;

alter table clean.outcomes add primary key (projectid);
