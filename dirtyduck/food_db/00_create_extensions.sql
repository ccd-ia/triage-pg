-- Extensions for the DirtyDuck fixture, trimmed to what the pipeline uses (DB-audit #4, ADR-0003).
-- triage-pg targets plain PostgreSQL that runs identically on a laptop and RDS, so we load only
-- the extensions a feature actually needs.

-- Spatial: backs the geography `location` column (the only extension currently load-bearing).
create extension if not exists postgis;

-- Text toolkit — kept for *potential* violation-description / fuzzy-match features (deferred,
-- DB-audit #5/§7); light and RDS-available. Remove if text features are ruled out.
create extension if not exists pg_trgm;
create extension if not exists fuzzystrmatch;
create extension if not exists unaccent;

-- Dropped vs the old fixture (unused; several RDS-restricted): postgis_raster, postgis_topology,
-- postgis_sfcgal, bloom, cube, citext (enums enforce casing now), earthdistance, file_fdw,
-- postgres_fdw.
