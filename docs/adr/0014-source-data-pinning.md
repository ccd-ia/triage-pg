# 0014. Source data enters identity via declared registry pins

- Status: Accepted
- Date: 2026-06-11

Source tables cannot be content-hashed cheaply, so they enter derivation hashes
(ADR-0013) as **declared pins**: configs explicitly declare the source tables
they read (no SQL parsing), a `triage.sources` / `triage.source_versions`
registry records a `version_label` per load (bumped by the ETL or `triage
source bump`), and at plan time the adapter freezes the current
`(source_name, version_label)` pairs into every downstream hash, recording them
per run (`triage.run_source_pins` — the `guix describe` analog). A declared but
**unpinned** source is treated as volatile: derivations touching it are never
cache hits and trigger a loud warning — the failure mode is a wasted rebuild,
never a silently stale cache. Cheap fingerprints (`row_count`,
`max(knowledge_date_column)`) are captured **advisory-only** to warn when data
moved but the pin didn't; they never enter identity.

## Considered alternatives
- *Config-inline version stamps* — rejected: per-experiment copies drift;
  manual `replace=True` with extra steps.
- *Automatic fingerprint-as-identity* — rejected: unsound (backfills can leave
  row count and max date unchanged ⇒ false cache hits).
- *Full content hashing of source tables* — rejected: prohibitive at
  consulting scale.
- *Per-table version-counter triggers* — rejected: invasive DDL on user data
  for little gain over loader bumps.

## Consequences
- Loaders gain one obligation: bump the pin when a load lands (CLI provided);
  forgetting it costs rebuilds (volatile) or a drift warning, never wrong reuse.
- Teaching/DirtyDuck quickstarts run unpinned with warnings — no setup friction.
- Reproducing a past run's inputs is a SQL query over `run_source_pins`.
