# 0023. Feature groups + mixing strategies (leave-one-out / -in / all / all-combinations)

- Status: Accepted
- Date: 2026-06-28
- Deciders: Adolfo, Claude

Original DSSG triage lets you partition the feature set into **feature groups** and sweep
**strategies** over them — `all`, `leave-one-out`, `leave-one-in`, `all-combinations`
(`triage/component/architect/feature_group_mixer.py`) — the standard tool for asking "does
this *block* of features actually add lift?" triage-pg dropped it in the greenfield rewrite
and no ADR recorded a decision. This ADR re-adds it with full parity, defined natively over
**featurizer**'s output and mapped onto the ADR-0022 Experiment/Run split.

## Decision

**1. A feature group is a named partition of featurizer's output feature columns.**
featurizer has no first-class "group" concept; it emits columns from an ER-graph. Two ways
to define groups, both at the triage-pg adapter layer (featurizer stays group-agnostic,
ADR-0008):

- **`group_by: source_entity` (default).** Partition columns by the *source* entity encoded
  in each feature name — `facilities.facility_type=…` → `facilities`,
  `COUNT(inspections.result|interval=P3M)` → `inspections`. NOTE: this uses the entity in the
  feature *name*, **not** featurizer's manifest `entity` field, which stamps every aggregation
  with the *target* entity (`entity=parent`) and would collapse everything into one group.
  DirtyDuck → `{facilities, inspections}`.
- **`definitions:` (explicit).** A map of `group_name → [column-name globs]`, for splitting one
  entity's features into sub-groups (counts vs risk-mix) or merging entities. Overrides
  `group_by`. Every feature column must land in exactly one group; unmatched columns are an
  error (loud, not silent — a typo'd glob shouldn't silently drop features).

**2. Strategies sweep the groups, producing feature-column subsets** (ported verbatim from
triage's `FeatureGroupMixer`):
- `all` — one subset: all groups.
- `leave-one-in` — one subset per group (each group alone).
- `leave-one-out` — one subset per group (all groups except that one).
- `all-combinations` — every non-empty subset (2^N − 1). Guarded: error if N groups would
  exceed a configured cap (default 6 → 63 subsets) so a fat ER-graph can't silently explode.

**3. Each subset is a Run, not an Experiment (ADR-0022).** The Experiment hash
(cohort+label+temporal+problem_type) is identical across subsets; the feature subset is part of
the Run's *attempt*. One `triage run` with strategies **fans out into N Runs** under one
Experiment, so their leaderboards are directly comparable (same labels, same splits).

**4. Mechanism: column projection of a single featurizer pass; the subset enters the MODEL
identity.** featurizer runs once per split (the full `feature_group` + `matrix` artifacts are
built once, under the first run, and shared via the DAG cache + `run_artifacts` usage edges).
Each subset is then a pure **column projection** of that one Parquet: a `MatrixResult` carrying
the same `storage_uri` but the subset's `feature_names`. There is **no per-subset matrix node or
projected Parquet copy** — instead the subset (a sorted `feature_list`) enters the **model
artifact identity** (and the `model_group` hash), so two subsets over the shared full matrix mint
distinct models / model-groups (and the read dashboard groups them separately for comparison).
Fit-based imputation (ADR-0009) was already applied per-column in the full matrix, and imputation
is per-column independent, so a projected subset needs no re-fit — the leakage boundary is
unchanged. `feature_groups` is nested under `feature_config` for authoring convenience but is
**stripped** by the adapter before featurizer (or the `feature_group` node identity) sees it.

## Considered alternatives

- *Re-run featurizer per subset* — rejected: wasteful (featurizer is the costly step; the subsets
  are pure column projections of the same pass) and it would not change identities meaningfully.
- *Group by featurizer's manifest `entity` field* — rejected: it stamps aggregations with the
  target entity, collapsing DirtyDuck to one group (see Decision 1).
- *Make featurizer aware of groups* — rejected: triage concepts must not leak into featurizer
  (ADR-0008). Grouping is a triage-pg adapter concern over featurizer's columns + manifest.

## Consequences

- New config block `feature_config.feature_groups` (`strategies`, `group_by`, optional
  `definitions`, optional `all_combinations_max_groups`). Absent ⇒ today's behaviour exactly:
  one implicit group, one Run (`n_feature_groups: 1`).
- New `triage.adapters.feature_groups` module (partition + the four strategies); `run_experiment`
  expands strategies → Runs.
- `all-combinations` is 2^N − 1 Runs × splits × grid — the documented blow-up; capped by default.
- Survives ADR-0022: groups change only the Run attempt, never the Experiment identity.
