-- Chicago 311 Service Requests — RAW layer (pg-data-discovery methodology).
-- Every column is TEXT: nothing fails at ingestion; types are fixed later in the clean layer.
-- COPY reads requests.csv at /data (a deterministic real-data subset baked into the image, or a
-- larger export mounted over /data — identical loader). The column list + order match the CSV
-- header exactly (a 21-column projection of the City's `v6vf-nfxy` dataset; see README + Dockerfile).

create schema if not exists raw;

drop table if exists raw.requests cascade;
create table raw.requests (
    sr_number          text,   -- service request id (e.g. SR19-00012345)
    sr_type            text,   -- request type ("Pothole in Street Complaint", ...)
    sr_short_code      text,
    owner_department   text,   -- department that owns resolution
    status             text,   -- Completed / Canceled / Open
    origin             text,   -- how it was filed (Phone Call, Internet, ...)
    created_date       text,   -- when the request was filed (knowledge date)
    closed_date        text,   -- when it was resolved (label source)
    last_modified_date text,
    street_address     text,
    zip_code           text,
    community_area     text,   -- Chicago community area number (1-77)
    ward               text,   -- aldermanic ward number
    police_district    text,
    latitude           text,
    longitude          text,
    created_day_of_week text,  -- 1=Sunday .. 7=Saturday (Socrata convention)
    created_hour       text,   -- 0-23
    created_month      text,   -- 1-12
    duplicate          text,   -- checkbox: a duplicate of another SR
    legacy_record      text    -- checkbox: migrated from the pre-2018 system
);

copy raw.requests from '/data/requests.csv' with (format csv, header true);
