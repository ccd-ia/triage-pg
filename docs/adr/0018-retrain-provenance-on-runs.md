# 0018. Retrain/forward-score provenance on `triage.runs`, not a dedicated table

- Status: Accepted
- Date: 2026-06-19
- Deciders: Adolfo De Unánue

## Context

Inherited triage persisted retrains in dedicated `Retrain`/`RetrainModel` ORM
tables carrying the `prediction_date` and `test_duration` of each retrain. The
greenfield rewrite (ADR-0001) deletes that ORM tree and replaces the
orchestration with the `adapters/` pipeline. Greenfield `triage.runs` records one
row per pipeline invocation but had no way to say *what kind* of run it was or
*what date* a retrain/forward-score targeted — every run looked like an
experiment.

The artifact DAG already captures a retrained model's full lineage: its train
matrix's `as_of_dates`/config encode the prediction cut. So the provenance is not
strictly *lost* without a record. But recovering "what prediction date was this
retrain for?" means knowing the `as_of = prediction_date - label_timespan`
convention and inverting it out of the train-matrix config — fragile, non-obvious,
and wrong if the convention ever changes.

## Decision

Capture retrain/forward-score provenance as two columns on `triage.runs`
(migration `0003`), not a separate `triage.retrains` table:

- `purpose text not null default 'experiment'` —
  `check (purpose in ('experiment', 'retrain', 'forward_score'))`. The default
  keeps `run_experiment`'s existing INSERT unchanged and backfills old rows.
- `prediction_date date` — NULL for experiment runs; set for retrain/forward
  runs to the date they served.

`adapters/retrain.py` writes `purpose='retrain'`; `adapters/forward.py` writes
`purpose='forward_score'`; both set `prediction_date`. "List retrains for a model
group and the dates they served" becomes a direct `triage.runs ⋈ triage.models`
query.

## Considered alternatives

- *Derive it from the DAG only (no record)* — rejected: forces every consumer to
  invert the `as_of`/`label_timespan` convention out of a train matrix; fragile
  and breaks silently if the convention changes.
- *A dedicated `triage.retrains` table* — rejected: more schema surface and a new
  FK to maintain for what is one discriminator + one date on a row that already
  exists for every run. Normalize onto `runs`.
- *Reintroduce the inherited `run_type` enum + Retrain/RetrainModel tables* —
  rejected: that ORM is exactly what ADR-0001 removed.

## Consequences

- One migration adds two nullable/defaulted columns; no data migration needed.
- The model-group/run join answers retrain history without DAG inversion.
- `purpose` is an open-coded `text` + `check` rather than a PG enum, matching the
  lighter-weight `runs.profile` style and keeping new purposes a one-line check
  change rather than an `ALTER TYPE`.
