# 0017. GC collects outputs, not history; usage-edge roots; predictions pin

- Status: Accepted
- Date: 2026-06-12

Garbage collection's default unit is the **output**, never the record: dead
artifacts (unreachable from any root) have their outputs deleted — Parquet
matrices, model files, in-PG cohort/label slices — and flip to
`status='collected'`, keeping rows, lineage, and pins for provenance; since
every closure is pinned (ADR-0013/0014), a collected artifact transparently
**rebuilds on its next cache miss**. Roots are computed from a new
`triage.run_artifacts` usage table (every artifact a run *used*, built or
cache-hit — `built_by_run` alone is not a liveness edge) restricted to
**non-archived** experiments (`experiments.archived_at`, soft archive via
`triage archive`), plus **predicted models**, which append-only predictions pin
regardless of experiment lifecycle; live = roots' upstream closure. GC is a
**manual CLI** (`triage gc`, dry-run by default, `--delete` / `--purge` /
`--min-age`) — keep-forever until invoked, matching ADR-0006. Row deletion is
an explicit bottom-up purge, backstopped by FK hardening: `predictions.model_id`
flips from the inherited CASCADE to **RESTRICT** (append-only becomes
DB-enforced) and `artifact_inputs.parent_id` is RESTRICT (an edge is the
child's provenance), while domain rows (`matrices`, `models`, `cohorts`,
`labels`) CASCADE from their artifact.

## Considered alternatives
- *Guix-style full deletion as the only mode* — rejected: destroys the
  provenance of past runs together with the cache.
- *Hard-deleting experiments as the root-removal gesture* — rejected:
  conflates "stop retaining" with "erase the record".
- *Keeping predictions ON DELETE CASCADE* — rejected: leaves the keep-forever
  guarantee (ADR-0006) to application-code discipline; a purge bug would
  silently eat history.
- *Scheduled auto-GC* — rejected: silent deletion of rebuildable-but-expensive
  artifacts is the wrong default, and file deletion needs the storage adapter,
  not pg_cron.

## Consequences
- Provenance is permanent by default; storage is reclaimable on demand;
  "collected" artifacts cost a rebuild, never a correctness loss.
- Builders must call `record_use()` for cache hits, not only builds — usage,
  not authorship, is what keeps artifacts alive.
- A dead parent with a surviving child is retained by purge until the child
  goes (bottom-up deletion).

## Status update (2026-06-20)

The storage-adapter seam this ADR deferred has landed. `collect()`
(`triage/artifacts.py`) deletes in-PG cohort/label slices, marks rows
`'collected'`, and returns file-backed outputs as `{artifact_id, kind,
output_ref}`; `delete_outputs()` / `_delete_output_file()` then remove the
files — local FS via `Path.unlink`, `s3://` via `s3fs` — fail-fast on real I/O
errors and tolerant of already-absent files. Both are wired into `triage gc
--delete` (`cli.py`) and covered by `src/tests/test_gc.py`. A feature_group
consumed inline as Arrow carries the sentinel `output_ref="featurizer:feature_group"`
(no file, no slice) and is collected without an external-deletion attempt; a
future `to_tables` materialization would carry a real table ref needing a
`DROP TABLE` handler.
