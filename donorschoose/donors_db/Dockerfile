# DonorsChoose (KDD Cup 2014) database — PostgreSQL 16.
# Ships a small REAL-DATA SUBSET (~3,000 projects posted 2012-2013, sampled deterministically,
# + their donations / resources / outcomes) baked in at /data, so the image is self-contained
# and runnable (~8 MB) without the ~1.6 GB Kaggle download. To run on the FULL data, mount the
# real Kaggle CSVs over /data (projects.csv / donations.csv / resources.csv / outcomes.csv) —
# the loader (01_create_raw.sql, COPY FROM '/data/*.csv') is identical. See README.md.

FROM postgres:16

# Default data = the real-data subset (overridden by mounting the full CSVs over /data).
COPY sample/projects.csv  /data/projects.csv
COPY sample/donations.csv /data/donations.csv
COPY sample/resources.csv /data/resources.csv
COPY sample/outcomes.csv  /data/outcomes.csv

# Init scripts run in order at first start: raw (text) -> clean (typed) -> ontology (E-S-E).
COPY 01_create_raw.sql     /docker-entrypoint-initdb.d/
COPY 02_create_clean.sql   /docker-entrypoint-initdb.d/
COPY 03_create_ontology.sql /docker-entrypoint-initdb.d/

RUN chown postgres:postgres /docker-entrypoint-initdb.d/*.sql /data/*.csv
