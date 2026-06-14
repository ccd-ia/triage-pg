# Adapter spec — the triage-pg ↔ featurizer seam

triage-pg owns the glue that maps triage concepts onto featurizer, storage, auth, and
execution. This document specifies that glue, one adapter at a time. It is the home of the
**adapter-spec pass** deferred from `docs/schema-design.md` §8 (the three items below).

| # | Adapter | Status |
|---|---------|--------|
| 1 | timechop `temporal_config` (the as_of_date/split generator) | **specified (this doc)** |
| 2 | featurizer ER-graph config + cohort→target mapping | stub — TODO |
| 3 | imputation policy wiring (ADR-0009) | stub — TODO |

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

## 2. featurizer ER-graph config + cohort→target mapping  *(stub — TODO)*

How cohort rows become featurizer's DFS target; the entity-graph config section (ADR-0008).

## 3. imputation policy wiring  *(stub — TODO)*

Fit-free imputation in featurizer; fit-based (train-split-fitted) imputation in the
triage-pg adapter — the leakage boundary (ADR-0009).
