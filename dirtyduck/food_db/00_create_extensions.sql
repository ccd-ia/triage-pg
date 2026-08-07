-- Extensions for the DirtyDuck fixture, trimmed to what the pipeline uses (DB-audit #4, ADR-0003).
-- triage-pg targets plain PostgreSQL that runs identically on a laptop and RDS, so we load only
-- the extensions a feature actually needs.

-- Spatial: backs the geography `location` column (the only extension currently load-bearing).
create extension if not exists postgis;

-- Text toolkit — the deferred violation-text features landed 2026-08-07 as plain word-bounded
-- regex flags over the inspector comment (04_create_semantic_tables.sql kw_* columns; consumed by
-- example/dirtyduck/experiment-violations.yaml). These extensions stay for fuzzy-match /
-- similarity exploration beyond the fixed keyword set; light and RDS-available.
create extension if not exists pg_trgm;
create extension if not exists fuzzystrmatch;
create extension if not exists unaccent;

-- Dropped vs the old fixture (unused; several RDS-restricted): postgis_raster, postgis_topology,
-- postgis_sfcgal, bloom, cube, citext (enums enforce casing now), earthdistance, file_fdw,
-- postgres_fdw.
