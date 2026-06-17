# featurizer scale validation — the per-as_of_date CTE cost (ADR-0008)

**Status:** validated 2026-06-17 · **Verdict:** (a) scalable as-is for our
realistic volumes, with a known featurizer-side optimization filed for later if
volumes grow. · **Benchmark:** [`benchmarks/featurizer_scale.py`](../benchmarks/featurizer_scale.py)

## What this validates

ADR-0008 adopts **featurizer** as the feature engine and records one open risk
as its *main* one:

> featurizer re-evaluates aggregation CTEs once per as_of_date with no reuse
> across dates, so its generated SQL must be benchmarked on realistic volumes
> *during* feature-pipeline integration. If it can't scale and can't be fixed,
> revisit Collate.

featurizer renders its matrix as a single lateral query
([`featurizer/sql.py`](../../featurizer/featurizer/sql.py)):

```sql
select aod.as_of_date, t.*
from as_of_dates as aod
cross join lateral (
    with <aggregation CTEs over the entity graph>
    select * from <target>_transform
) as t
order by aod.as_of_date
```

The `cross join lateral` means the **entire aggregation CTE body is
re-evaluated once per row of `as_of_dates`** — there is no cross-date reuse of
the windowed aggregates. The make-or-break question is therefore:

- Does wall-clock grow **linearly** in the number of as_of_dates (acceptable —
  you simply pay per date), or
- **superlinearly** (the real danger — the per-date cost itself rises as you add
  dates, e.g. because the planner or the lateral re-scan degrades)?

This note answers that empirically.

## Methodology

The benchmark is a standalone, reproducible script — **not** a CI test. It:

1. Spins its **own throwaway PostgreSQL 16 cluster** in a temp datadir via
   `pytest_postgresql`'s `PostgreSQLExecutor` (the same mechanism the test
   suite uses), driven directly outside pytest, and torn down on exit. It never
   touches the host `PG*` environment.
2. Generates synthetic relational data in the canonical public-policy shape: a
   **target entity** table `clients(client_id, enrolled_on, region,
   baseline_score)` + one **child event stream** `visits(visit_id, client_id,
   visited_on, amount, service)`, with `enrolled_on` / `visited_on` as the
   point-in-time `temporal_ix` knowledge dates and a `(client_id, visited_on)`
   index on the child (what the as-of join filters on). Parameterized by
   `N_entities` and `events_per_entity`.
3. Runs a fixed featurizer config that is deliberately **non-trivial** so the
   CTE body is realistic, not a toy:
   - `max_depth: 2`
   - `intervals: [P30D, P90D, P180D]` (3 rolling windows)
   - `aggregations: [count, sum, mean, max, stddev]` (5)
   - numeric + categorical child variables (`amount`, `service`)
   - one parent→child relationship
   - → **33 output columns** per matrix.
4. For each scale point, times:
   - **SQL generation** — rendering `Featurizer.query` (planner + SQL renderer).
   - **SQL execution** — wall-clock to run the generated query to completion,
     materialized into a `CREATE TEMP TABLE ... AS` so the server evaluates
     every per-as_of_date CTE and writes all rows (paying the full cost) without
     shipping rows to the Python client; plus the server-side `EXPLAIN ANALYZE`
     "Execution Time" for cross-check. Best-of-2 repeats per point.

Two axes:

- **PRIMARY** — hold entities/events fixed (20,000 entities × 30 events =
  600,000 visit rows), vary `#as_of_dates ∈ {1, 5, 10, 20, 40}`. This isolates
  the per-as_of_date cost.
- **SECONDARY** — hold `#as_of_dates = 12`, vary `#entities ∈ {1k, 10k, 100k}`
  (30 events each, so up to 3,000,000 visit rows).

**Reproduce:**

```bash
uv run python benchmarks/featurizer_scale.py            # full (numbers below)
uv run python benchmarks/featurizer_scale.py --quick    # fast smoke scales
```

**Hardware / version caveat.** Numbers below were collected on an Apple M5 Max
(18 logical cores, 128 GB RAM), macOS 26.5.1, PostgreSQL 16.14 (Homebrew) with
`shared_buffers=256MB, work_mem=64MB`. Absolute seconds are hardware-specific
and a laptop SSD / `shared_buffers` will move them; the **scaling shape** (the
ratios and the per-date trend) is what transfers, and is what the verdict rests
on. A loaded RDS instance under concurrency will be slower in absolute terms.

## Results

### PRIMARY — execution time vs #as_of_dates (20,000 entities, 600,000 visits, 33 cols)

| #as_of_dates | exec (s) | explain (s) | s / as_of_date | vs 1 date |
|--------------|----------|-------------|----------------|-----------|
| 1            | 1.421    | 1.412       | 1.421          | 1.00x     |
| 5            | 6.836    | 6.673       | 1.367          | 4.81x     |
| 10           | 12.839   | 12.832      | 1.284          | 9.04x     |
| 20           | 23.426   | 23.305      | 1.171          | 16.49x    |
| 40           | 38.229   | 38.023      | 0.956          | 26.90x    |

Per-as_of_date cost: **mean 1.24 s, coefficient of variation 13.3%** — and the
trend is *monotonically declining* (1.42 s → 0.96 s per date). 40 dates cost
**26.9×** a single date, i.e. *less than* 40× — **sub-linear**.

### SECONDARY — execution time vs #entities (12 as_of_dates, 30 events/entity)

| #entities | #visits   | exec (s) | explain (s) |
|-----------|-----------|----------|-------------|
| 1,000     | 30,000    | 0.761    | 0.759       |
| 10,000    | 300,000   | 7.624    | 7.576       |
| 100,000   | 3,000,000 | 76.713   | 76.629      |

Clean **linear** scaling in entity/event volume: each 10× of entities is ~10×
the time (0.76 → 7.6 → 76.7 s).

## Scaling characterization

- **Per-as_of_date cost is constant-to-sub-linear.** The danger scenario
  (per-date cost *rising* as dates are added) does **not** occur. If anything it
  falls slightly — adding dates lets PostgreSQL reuse the warm `visits` heap/
  index across the lateral re-scans, so later dates are marginally cheaper. The
  `cross join lateral` re-evaluation is therefore the *expected* linear cost, not
  a superlinear trap.
- **Total cost ≈ `k · #entities · #as_of_dates`** (the matrix has exactly that
  many rows, and each row costs a bounded as-of aggregation over the child
  windows). Both axes confirm this: doubling dates ≈ doubles time, 10×-ing
  entities ≈ 10×-es time.
- **SQL generation is free** (~1–2 ms regardless of scale) — generation does not
  depend on `#as_of_dates`, it renders the same lateral query and PostgreSQL
  expands it over the runtime `as_of_dates` table.

### Where it becomes impractical

Extrapolating the linear model `time ≈ #entities × #dates × ~6.4 µs/cell`
(from the 100k × 12 = 1.2M-cell @ 77 s point) for *this* 33-column config on
*this* hardware:

| realistic target                         | matrix cells | est. single-pass build |
|-------------------------------------------|--------------|-------------------------|
| 100k entities × 12 dates                  | 1.2M         | ~77 s (measured)        |
| 100k entities × 40 dates                  | 4.0M         | ~4 min                  |
| 1M entities × 12 dates                    | 12M          | ~13 min                 |
| 1M entities × 40 dates                    | 40M          | ~45 min                 |

So for our stated realistic public-policy envelope — **~10^5 entities × ~10–40
as_of_dates** — a single matrix build is **seconds to a few minutes**:
comfortably practical. It only becomes uncomfortable around **10^6 entities ×
many dates with wide configs** (tens of minutes per matrix), and even then it
degrades *gracefully and linearly*, not off a cliff. Wider configs (more
intervals × aggregations × deeper graph) scale the per-cell constant, not the
shape; PostgreSQL's 1664-columns-per-row limit is the practical width ceiling
(noted in featurizer's own example configs), not runtime.

## Verdict

**(a) Scalable as-is for our realistic volumes.** The benchmarked scaling is
linear-in-entities and constant-to-sub-linear-in-as_of_dates — i.e. the
`cross join lateral` re-evaluation behaves as the *benign* linear cost, not the
feared superlinear one. ADR-0008's escape hatch ("revisit Collate") is **not**
needed. Recommendation: **proceed with featurizer** as the feature engine; no
change to the adapter (`src/triage/adapters/matrix.py`) is warranted.

**Future headroom — a featurizer-side optimization to file (not fix here).**
The per-date re-evaluation is wasted work whenever windows overlap across
nearby as_of_dates (our 30/90/180-day intervals over monthly dates re-scan the
same `visits` rows up to 6×). At the 10^6-entities-× -many-dates frontier, where
builds reach tens of minutes, featurizer could **materialize the per-entity
aggregation once and join it per-date** instead of re-deriving the CTE inside
the lateral — i.e. precompute the windowed aggregates keyed by
`(entity, as_of_date)` in a single pass (a date-bucketed group-by / range-join
or a `WITH ... MATERIALIZED` aggregate reused across dates) rather than
`cross join lateral`. That is an **engine-side change in featurizer**, must not
leak triage concepts, and should be **filed as a featurizer issue** ("reuse
aggregation work across as_of_dates instead of per-date lateral
re-evaluation"), to be picked up only if/when real workloads exceed the
few-minute range. It is an optimization, not a correctness fix, and is out of
scope for this validation (which only measures and documents — it does not
modify featurizer or the adapter).

## Reproducibility

- Script: [`benchmarks/featurizer_scale.py`](../benchmarks/featurizer_scale.py)
  (self-contained; spins its own Postgres; `--quick` for a fast run).
- Raw output that produced the tables above is pasted verbatim — timings are
  measured, not estimated.
