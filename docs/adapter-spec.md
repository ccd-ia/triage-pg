# Adapter spec — the triage-pg ↔ featurizer seam

triage-pg owns the glue that maps triage concepts onto featurizer, storage, auth, and
execution. This document specifies that glue, one adapter at a time. It is the home of the
**adapter-spec pass** deferred from `docs/schema-design.md` §8 (the three items below).

| # | Adapter | Status |
|---|---------|--------|
| 1 | timechop `temporal_config` (the as_of_date/split generator) | **specified (this doc)** |
| 2 | featurizer ER-graph config + cohort→target mapping | **specified (this doc)** |
| 3 | imputation policy wiring (ADR-0009) | **specified (this doc)** |

The model code for §1 lives at `src/triage/adapters/temporal.py`
(`triage.adapters.TemporalConfig`); its tests at `src/tests/adapter_tests/test_temporal_config.py`.

---

## 1. timechop `temporal_config`

### 1.1 Decision

Keep the inherited timechop temporal semantics **unchanged** — they encode leakage-safe
temporal cross-validation that is correct and battle-tested; reinventing them is risk
without reward. The adapter contribution is a **typed, validated, canonical front door**
(`TemporalConfig`, a pydantic model) that the rest of triage-pg constructs and passes to
the unmodified `Timechop` engine (`src/triage/component/timechop/timechop.py`). timechop
**stays** as the generator (schema-design §8.5); this model does not replace it.

### 1.2 Fields

The eleven inherited keys, with types. Dates are `YYYY-MM-DD`. Intervals are Postgres
interval strings (`'6month'`, `'1 day'`, `'2years'`, `'0day'`); the six list-valued fields
accept a single interval **or** a list of them.

| Field | Type | Meaning |
|---|---|---|
| `feature_start_time` | date | Earliest date included in any feature. |
| `feature_end_time` | date | **Day after** the last feature date (half-open). |
| `label_start_time` | date | Earliest date for which labels are available. |
| `label_end_time` | date | **Day after** the last label date (half-open). |
| `model_update_frequency` | interval | How often to retrain (the train/test split step-back). |
| `training_as_of_date_frequencies` | interval \| list | Time between rows for one entity in a **train** matrix. |
| `test_as_of_date_frequencies` | interval \| list | Time between rows for one entity in a **test** matrix. |
| `max_training_histories` | interval \| list | Lookback: how far back from a split to pull training rows. |
| `test_durations` | interval \| list | How far past a split to predict (test-matrix length; `'0day'` = one prediction at the split). |
| `training_label_timespans` | interval \| list | Time aggregated for a label in **train** matrices. |
| `test_label_timespans` | interval \| list | Time aggregated for a label in **test** matrices. |

**Convenience:** when train and test label spans are equal, supply a single
`label_timespans` key instead; it expands to both `training_label_timespans` and
`test_label_timespans` (mirrors `triage.experiments.defaults`). Unknown keys are rejected.

A representative block (`example/config/experiment.yaml`):

```yaml
temporal_config:
    feature_start_time: '1995-01-01'
    feature_end_time: '2015-01-01'
    label_start_time: '2012-01-01'
    label_end_time: '2015-01-01'
    model_update_frequency: '6month'
    training_as_of_date_frequencies: '1day'
    test_as_of_date_frequencies: '3month'
    max_training_histories: ['6month', '3month']
    test_durations: ['0day', '1month', '2month']
    training_label_timespans: ['1month']
    test_label_timespans: ['7day']
```

### 1.3 Conventions

- **Half-open windows.** `*_end_time` is the day *after* the last included date — features
  use data strictly before `feature_end_time`; no model spans a date `>= label_end_time`.
- **Interval grammar.** Parsed by `triage.util.conf.convert_str_to_relativedelta`: an
  integer then a unit (`year[s]`/`month[s]`/`day[s]`/`week[s]`/`hour[s]`/`minute[s]`/
  `second[s]`/`microsecond[s]`, or the abbreviations `y d w h m s ms`). **`m` is minutes,
  not months** — months must be spelled out.
- **Scalar-or-list.** The six list fields coerce a bare interval to a one-element list
  (`triage.component.timechop.utils.convert_to_list`).
- **Canonical interval tokens.** Each interval is normalized to `"<n> <unit>s"` (e.g.
  `'6month'` and `'6 months'` both → `"6 months"`). This is what makes the config's
  serialization stable for hashing (§1.6). Normalization is semantics-preserving:
  `convert_str_to_relativedelta(canonical(x)) == convert_str_to_relativedelta(x)`.

### 1.4 Validation rules

1. `feature_start_time <= feature_end_time` and `label_start_time <= label_end_time`
   (enforced by the model; also re-checked inside `Timechop`).
2. Every interval parses; every list field is non-empty.
3. Unknown keys rejected (`extra="forbid"`) so a typo fails loudly.
4. **No-leakage invariant** (a *post-generation* check, not a field rule): for every split,
   each test `as_of_date` must be `>= max(train as_of_dates) + training_label_timespan`.
   This requires running the generator, so it is verified after `chop_time()` by
   `triage.experiments.validate.TemporalValidator` (retained); the model documents it but
   cannot enforce it field-wise.

### 1.5 Mapping onto the schema

`Timechop.chop_time()` yields split definitions (`train_matrix` + one or more
`test_matrices`, each carrying `as_of_times`, a `label_timespan`, a frequency, and — for
train — a `max_training_history`). The adapter maps each onto the `triage` schema:

- Each generated matrix → a `triage.matrices` row with `matrix_kind` ∈ `split_kind`
  (`train` / `test` now; `validation` / `production` reserved for later), `label_timespan`
  (from the split's timespan) and `lookback` (from `max_training_history`).
- Each split's `as_of_times` become featurizer's `as_of_dates`, and fill the `{as_of_date}`
  placeholder in the templated cohort/label SQL; the label span fills `{label_timespan}`.
- Predictions and evaluations carry the same `split_kind` discriminator.

### 1.6 problem_type coupling and derivation identity

- **problem_type is not part of `temporal_config`.** It lives at the experiment level and
  dictates the label query's required columns (`outcome` | `duration, event_observed`,
  ADR-0010). `temporal_config` only supplies the `{label_timespan}` the label template
  needs; the two compose at matrix-assembly time.
- **`temporal_config` enters artifact identity.** Its canonical form
  (`TemporalConfig.canonical()`) is part of the config slice that
  `triage.derivation.derive` hashes for cohort / labels / matrix nodes
  (`docs/derivation-dag.md` §2). Because intervals are canonical tokens and dates are ISO
  strings, two configs that differ only in surface spelling or scalar-vs-list form hash
  identically — and any *semantic* change invalidates the downstream closure by construction.

### 1.7 Model surface

`triage.adapters.TemporalConfig` (frozen pydantic model):

- `TemporalConfig.model_validate(cfg)` / `TemporalConfig(**cfg)` — build + validate from a raw dict.
- `.to_timechop_kwargs()` — kwargs for the unmodified `Timechop(**kwargs)` (dates as ISO
  strings, intervals as canonical tokens — both valid engine input).
- `.canonical()` — deterministic, JSON-serializable dict for derivation hashing.

---

## 2. featurizer ER-graph config + cohort→target mapping

> **Moving target.** featurizer (`~/projects/featurizer`) is under active development
> (graph/sequence primitives, etc.). Its maintainer's read (2026-06-14): the **config file
> is almost stable**. This spec pins to the settled core schema (§2.2) and treats the
> in-flux pieces as opt-in (§2.9). Re-validate the §2.2 field list against featurizer
> before the adapter build.

### 2.1 Decision

featurizer owns the **ER-graph and DFS**: the user declares entities, relationships,
variables, intervals, and primitives in a featurizer config, and featurizer generates the
point-in-time-correct feature SQL. triage-pg does **not** wrap or fork that config — it
passes it through near-verbatim and owns only the **seam**: the `target` entity, the
`as_of_dates` it computes for, the cohort selection, and the point-in-time contract.
Triage concepts must never leak into featurizer (ADR-0008).

### 2.2 The featurizer config (settled core)

Top-level keys (featurizer `featurizer/featurizer.yaml`, validated by
`featurizer/validation.py`):

| Key | Type | Required | Meaning |
|---|---|---|---|
| `target` | string | yes | Entity alias to compute features **for** (must be an `entities` alias). |
| `max_depth` | int | yes | Max DFS traversal depth across relationships. |
| `intervals` | list[ISO-8601] | yes | Global aggregation windows (`P7D`, `P1W`, `P1M`); overridable per variable. |
| `entities` | list[entity] | yes (≥1) | The nodes of the ER-graph. |
| `relationships` | list[rel] | no (`[]`) | The edges (parent→child, with optional `temporal` as-of join). |
| `aggregations` / `transformations` | list[string] | no | Primitive whitelist; omit for the default active set. |

Entity fields: `alias` (req), `table` (req, schema-qualified), `id` (PK column; `~` for a
keyless child), `temporal_ix` (event/knowledge timestamp — required for interval
aggregations), `spatial_ix` (optional; planner-unintegrated today), `variables`
(`{col: {type, intervals?, predicates?}}`, types `numeric|categorical|text|boolean|
date|timestamp|index|vector`), and the in-flux `edge:` / `peer_group:` blocks (§2.9).

Relationship fields: `parent: {entity, key}`, `child: {entity, key}`, and optional
`temporal: {mode: as_of, grace: <ISO-8601>, child_timestamp?}`.

A representative config is `featurizer/featurizer.yaml`; the smallest complete one is
featurizer's `examples/01-basic-aggregations/config.yaml`.

### 2.3 cohort → target mapping (the seam)

featurizer's rendered SQL (featurizer `featurizer/sql.py`) is:

```sql
select aod.as_of_date, t.*
from as_of_dates as aod
cross join lateral ( with <CTEs> select * from <target>_transform ) as t
order by aod.as_of_date
```

Three facts drive the mapping:

1. **`as_of_dates` is a runtime table, not config.** featurizer reads a table named
   `as_of_dates(as_of_date)` that must exist when the query runs (the config even says so:
   *“There is a table called as_of_dates”*). The **adapter materializes it** from the
   timechop split — `TemporalConfig` → `Timechop.chop_time()` → the split's `as_of_times`
   (§1) — one featurizer run per split-side.
2. **`target` is the cohort's entity.** The `target` entity's `id` column is triage's
   universal `entity_id`; its `table` is the entity/source table. featurizer computes a
   **dense** matrix: every target-entity row × every `as_of_date`, indexed
   `(as_of_date, entity_id)`.
3. **The cohort is a selection mask, applied after.** A triage cohort is a *per-as_of_date*
   roster (`triage.cohorts(cohort_hash, entity_id, as_of_date)`) — a **subset** of that
   dense product. The adapter selects it with an inner join:
   `featurizer_matrix INNER JOIN triage.cohorts USING (entity_id, as_of_date)` (filtered to
   the split's `cohort_hash`). Labels join the same way on `(entity_id, as_of_date)`
   (+`label_timespan`). This is the correct, no-featurizer-change v1 contract.

   **Open / scale (ADR-0008).** Computing features for all entities then discarding
   non-cohort rows is wasteful when the cohort is a small fraction. The optimization is a
   **cohort-scoped target**: make featurizer's outer relation the `(as_of_date, entity_id)`
   *cohort* pairs rather than `as_of_dates × all entities`. That is a **featurizer-side
   coordination item**, tied directly to the open featurizer-scale risk — not a triage-pg
   blocker, but the lever if scale validation fails.

### 2.4 Point-in-time correctness (the cardinal rule)

- `temporal_ix` must be the **knowledge date** (when a fact became known), not the event
  date (CLAUDE.md gotcha). The adapter sets each entity's `temporal_ix` from its
  `knowledge_date_column`.
- featurizer's as-of join currently bounds on `<= as_of_date - grace`. triage requires
  data knowable **strictly before** `as_of_date` (`<`). The `<=`→`<` boundary is a known,
  tracked **featurizer-side fix** (triage `TODO.org`, featurizer-side fixes) — flag it; do
  not silently rely on `<=`.
- `grace` (optional ISO-8601) is a lookback bound on a relationship's as-of join.

### 2.5 Intervals are a different axis from `temporal_config`

featurizer `intervals` (ISO-8601 aggregation windows: `P7D`, `P1M`) are **independent** of
timechop's `temporal_config` windows (`label_timespan`, `max_training_history`, §1). The
former bound *feature aggregation lookback*; the latter bound *labels and training
history*. Note also the **unit-grammar mismatch**: featurizer is ISO-8601; timechop is
Postgres-interval (`'6 months'`). They never share a value — keep them separate in config.

### 2.6 Imputation split (ADR-0009)

**Fit-free** imputation (zero/constant + `*_imp` flag columns) is featurizer's job —
`Featurizer.to_dataframe(impute=...)`. **Fit-based** imputation (mean/median/mode, fitted on
the *training split only*) is the triage-pg adapter's job and is the leakage boundary; its
wiring is §3. featurizer must never fit a statistic over the full matrix.

### 2.7 Feature groups & derivation identity (ADR-0015/0016)

featurizer is **monolithic per run** (one config → one matrix of all features). A triage-pg
**feature group** is an adapter-defined featurizer (sub-)config and is one `feature_group`
node in the derivation DAG. Its identity hashes: the **canonical featurizer config slice**,
its parents (cohort + the source-data pins of the tables it reads), and the **featurizer
engine version** (`engine_versions_for('feature_group')` already adds it). Changing the
config *or* the featurizer release invalidates the feature-group closure by construction —
which is exactly why featurizer's version must be release-pinned (ADR-0016).

### 2.8 Output & matrix assembly

featurizer yields the `(as_of_date, entity_id)`-indexed feature matrix — today as a pandas
DataFrame (`to_dataframe()`) or as raw SQL (`Featurizer.query`). The adapter assembles
**cohort ⋈ features ⋈ labels** into the design matrix and writes Parquet to
`triage.matrices.storage_uri` (matrices live on FS/S3, not in PG). Prefer consuming
`Featurizer.query` and going **SQL → Parquet** over materializing pandas (consistent with
the “drop pandas as a data-movement layer” cleanup in `TODO.org`).

### 2.9 Stability — what to rely on vs. validate

- **Rely on now:** `target`, `max_depth`, `intervals` (global + per-variable), `entities`
  (`alias`/`table`/`id`/`temporal_ix`/`variables`), `relationships` (incl. `temporal: as_of`
  + `grace`), `aggregations`/`transformations` whitelists, the `as_of_dates` runtime
  contract, and the rendered lateral-join SQL.
- **In flux — opt-in, validate before depending:** edge-table **graph** features (`edge:`),
  **sequence/Markov** aggregators, **`peer_group`** (proposed, not finalized), and
  **`spatial_ix`** (parsed but planner-unintegrated).

### 2.10 Open coordination items (featurizer-side)

1. **Cohort-scoped target** (§2.3) — the scale optimization; pursue if ADR-0008 scale
   validation fails.
2. **`<=`→`<` as-of boundary** (§2.4) — required for strict point-in-time correctness.
3. **SQL vs DataFrame output** (§2.8) — confirm triage-pg consumes `Featurizer.query`
   (SQL→Parquet), not `to_dataframe()`.

## 3. imputation policy wiring

### 3.1 Decision

Imputation is split along a **leakage boundary** (ADR-0009): **fit-free** rules
(zero/constant/`null_category` + the `*_imp` flag) compute nothing from data and are safe
anywhere; **fit-based** rules (mean/median/mode) compute a statistic that must be fitted on
the **training split only** and applied to both train and test — only triage-pg knows the
timechop split, so fit-based imputation is the adapter's job and *is* the boundary.

**Mechanism (locked):** featurizer emits NULL-preserving features (its default —
“missingness is signal”); the triage-pg adapter applies **all** fills — both fit-free and
fit-based — in **one SQL pass over `Featurizer.query`** (SQL → Parquet, no pandas; §2.8).

> **ADR-0009 refinement, recorded.** ADR-0009 assigns fit-free imputation to featurizer.
> featurizer *does* own the fit-free semantics (and offers them on its pandas
> `to_dataframe(impute=…)` path), but that path is off triage-pg's SQL→Parquet line. So the
> adapter **re-applies the fit-free fills in SQL** rather than calling featurizer's pandas
> pass. The ADR's actual purpose — the *fit-based* leakage boundary — is unchanged: fit-based
> stays train-only, in the adapter. Only the *locus* of the (leakage-free) fit-free fill
> moves. Worth a one-line amendment to ADR-0009 when next touched.

### 3.2 Where the policy lives, and its vocabulary

Imputation rules live in triage-pg's **feature config**, never in the featurizer config —
triage concepts must not leak into featurizer (ADR-0008). Keep the inherited shape
(`src/triage/component/architect/feature_generators.py`, `example/config/experiment.yaml`):
a top-level `aggregates_imputation` / `categoricals_imputation` block (with an `all`
fallback) plus per-feature `imputation:` blocks; precedence **feature-level > `all`**.

| Rule | Kind | Fill |
|---|---|---|
| `zero` | fit-free | `COALESCE(col, 0)` + `_imp` flag |
| `zero_noflag` | fit-free | `COALESCE(col, 0)`, no flag |
| `constant` | fit-free | `COALESCE(col, value)` + flag (requires `value`) |
| `null_category` | fit-free | categoricals: NULL → its own category (no flag) |
| `mean` / `median` / `mode` | **fit-based** | train-split statistic, applied to train+test |
| `binary_mode` | **fit-based** | train-split `AVG(col) > 0.5` |
| `error` | (no fill) | raise if any NULL remains |

Rules are typed and classified by `triage.adapters.ImputationRule` /
`triage.adapters.ImputationPolicy` (§3.7).

### 3.3 featurizer guardrail

Use only featurizer's **fit-free** behavior (count→0, measures→NULL, the missing-indicator
flag). **Never** pass featurizer `measure_strategy="mean"/"median"`
(`featurizer/imputation.py`): it fits the statistic over the **full** matrix → exactly the
ADR-0009 leak. featurizer's flag column is named `<feature>__missing`; triage-pg standardizes
on `<feature>_imp` (ADR-0009 wording) — map one to the other if featurizer's flag is ever consumed.

### 3.4 Fit-based mechanism (the leakage boundary)

For each fit-based feature: compute the statistic **over the train matrix rows only** → a
per-feature scalar → persist it in `triage.matrices.metadata` (jsonb) → apply by `COALESCE`
to **both** the train and test matrices. **Never** recompute per `as_of_date` on the test
side — that is precisely the inherited collate bug (`AVG() OVER (PARTITION BY as_of_date)`
refit including test dates, `src/triage/component/collate/imputations.py`,
`spacetime.py:get_impute_create`). mode/median render as `MODE() WITHIN GROUP` /
`PERCENTILE_CONT` over the train split (adapter-build detail, §3.8).

### 3.5 Fit-free mechanism (adapter SQL)

`zero`/`constant` → `COALESCE(col, 0|value)`; `null_category` routes a categorical NULL to
its own indicator; the flag is `CASE WHEN col IS NULL THEN 1 ELSE 0 END AS <feature>_imp`,
computed **before** the fill; `zero_noflag` suppresses the flag; `error` emits no fill and
the assembly fails if a NULL survives. All of this is plain SQL over `Featurizer.query`.

### 3.6 DAG / leakage edge (ADR-0015, derivation-dag §4.5)

Fit-based imputation lives **inside the matrix node**. The **test matrix takes the train
matrix as a parent** — `triage.artifact_inputs(artifact_id = test_matrix, parent_id =
train_matrix)`, `parent_id` RESTRICT — so the train-fitted stats flow to test along an
explicit DAG edge: the leakage boundary *is* a dependency edge. The imputation policy is
part of the matrix node's config and enters its derivation hash (via
`ImputationPolicy.canonical()`), so changing policy rebuilds both matrices while reusing all
cached per-date cohort/label/feature nodes (a cheap re-join + fill).

### 3.7 Model surface

`triage.adapters.ImputationRule` (frozen pydantic): `type` (the §3.2 vocabulary), optional
`value` (required iff `constant`), `.kind` → `fit_free | fit_based | error`, `.fits_on_train`,
`.canonical()`. `triage.adapters.ImputationPolicy` (a `RootModel[dict[str, ImputationRule]]`,
the `aggregates_imputation` shape): `.resolve(metric)` (explicit rule, else `all`),
`.requires_fit()` (any fit-based → a train statistic is needed), `.canonical()`
(sorted, for the matrix-node hash).

### 3.8 Open items

- **featurizer `measure_strategy` hazard** (§3.3) — guard against its use (lint/contract).
- **Fit-based SQL** — the `MODE() WITHIN GROUP` / `PERCENTILE_CONT`-over-train rendering and
  the `matrices.metadata` stat layout are adapter-build details, not fixed here.
- ~~**ADR-0009 amendment** (§3.1) — record that fit-free fills run in adapter SQL on the
  SQL→Parquet path.~~ **Done (2026-06-20):** recorded as the "Refinement" section in
  `docs/adr/0009-imputation-split-fit-free-vs-fit-based.md`.
