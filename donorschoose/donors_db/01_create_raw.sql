-- DonorsChoose (KDD Cup 2014) — RAW layer (pg-data-discovery methodology).
-- Every column is TEXT: nothing fails at ingestion; types are fixed later in the clean layer.
-- COPY reads the CSVs at /data (a subset baked into the image, or the real Kaggle files mounted
-- over /data — identical loader). _source_file is recorded for lineage.

create schema if not exists raw;

drop table if exists raw.projects cascade;
create table raw.projects (
    projectid text, teacher_acctid text, schoolid text, school_ncesid text,
    school_latitude text, school_longitude text, school_city text, school_state text,
    school_zip text, school_metro text, school_district text, school_county text,
    school_charter text, school_magnet text, school_year_round text, school_nlns text,
    school_kipp text, school_charter_ready_promise text, teacher_prefix text,
    teacher_teach_for_america text, teacher_ny_teaching_fellow text,
    primary_focus_subject text, primary_focus_area text, secondary_focus_subject text,
    secondary_focus_area text, resource_type text, poverty_level text, grade_level text,
    fulfillment_labor_materials text, total_price_excluding_optional_support text,
    total_price_including_optional_support text, students_reached text,
    eligible_double_your_impact_match text, eligible_almost_home_match text, date_posted text
);

drop table if exists raw.outcomes cascade;
create table raw.outcomes (
    projectid text, is_exciting text, at_least_1_teacher_referred_donor text,
    fully_funded text, at_least_1_green_donation text, great_chat text,
    three_or_more_non_teacher_referred_donors text,
    one_non_teacher_referred_donor_giving_100_plus text, donation_from_thoughtful_donor text,
    great_messages_proportion text, teacher_referred_count text, non_teacher_referred_count text
);

drop table if exists raw.resources cascade;
create table raw.resources (
    resourceid text, projectid text, vendorid text, vendor_name text,
    project_resource_type text, item_name text, item_number text,
    item_unit_price text, item_quantity text
);

drop table if exists raw.donations cascade;
create table raw.donations (
    donationid text, projectid text, donor_acctid text, donor_city text, donor_state text,
    donor_zip text, is_teacher_acct text, donation_timestamp text, donation_to_project text,
    donation_optional_support text, donation_total text, dollar_amount text,
    donation_included_optional_support text, payment_method text,
    payment_included_acct_credit text, payment_included_campaign_gift_card text,
    payment_included_web_purchased_gift_card text, payment_was_promo_matched text,
    via_giving_page text, for_honoree text, donation_message text
);

copy raw.projects  from '/data/projects.csv'  with (format csv, header true);
copy raw.outcomes  from '/data/outcomes.csv'  with (format csv, header true);
copy raw.resources from '/data/resources.csv' with (format csv, header true);
copy raw.donations from '/data/donations.csv' with (format csv, header true);
