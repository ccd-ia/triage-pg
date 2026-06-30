# Chicago 311 Service Requests database — PostgreSQL 16.
# Ships a deterministic REAL-DATA SUBSET (30,654 service requests filed in 2019, five field-service
# request types across three community areas — Austin, Logan Square, Roseland — ordered by
# sr_number) baked in at /data, so the image is self-contained and runnable (~9 MB) with no
# download. To run on a larger / different export, mount a CSV with the same 21-column header over
# /data/requests.csv — the loader (01_create_raw.sql, COPY FROM '/data/requests.csv') is identical.
# See README.md for the exact Socrata query that reproduces the subset.

FROM postgres:16

# Default data = the baked real-data subset (overridden by mounting a CSV over /data/requests.csv).
COPY sample/requests.csv /data/requests.csv

# Init scripts run in order at first start: raw (text) -> clean (typed) -> ontology (E-S-E).
COPY 01_create_raw.sql      /docker-entrypoint-initdb.d/
COPY 02_create_clean.sql    /docker-entrypoint-initdb.d/
COPY 03_create_ontology.sql /docker-entrypoint-initdb.d/

RUN chown postgres:postgres /docker-entrypoint-initdb.d/*.sql /data/*.csv
