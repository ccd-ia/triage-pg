# 0020. Defer in-container grid-search parallelism; keep the grid serial

- Status: Accepted
- Date: 2026-06-20

ADR-0005 envisioned grid-search parallelism as in-container multiprocessing, but the
grid×split loop (`adapters/run.py`) is **serial** today, and we are keeping it that way for
now. Naive multiprocessing would re-introduce exactly the cross-fork database-pool sharing
and estimator/connection pickling that removing `SerializableDbEngine` (ADR-0019) just
eliminated — so parallelism, which is a throughput knob and not a correctness requirement,
is deferred until there is a real throughput need and can then be designed deliberately
(each worker opens its **own** psycopg pool, re-loads its matrix Parquet from the storage
adapter, and shares no live connection objects across the fork).

## Consequences
- A cloud Batch job runs its grid serially; size the container's vCPU/memory accordingly,
  or split the grid across experiments, until this lands.
- ADR-0005's "grid-search parallelism stays as in-container multiprocessing" is now a
  *future* optimization gated by this ADR, not an implemented property.
- The execution adapter seam (`docs/cloud-profile-spec.md` §4) is independent of grid
  parallelism, so adding it later changes only the inner loop, not the local/cloud split.
