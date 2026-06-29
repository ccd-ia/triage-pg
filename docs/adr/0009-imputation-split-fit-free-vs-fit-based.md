# 0009. Imputation split: fit-free in featurizer, fit-based in triage-pg

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-28): Implemented — fit-free flags in featurizer; fit-based train-only stats persisted to `matrices.metadata` and reused by the test split via the train-matrix parent edge (leakage boundary property-tested); `adapters/imputation.py`.

Imputation is deliberately split across the two repos along a leakage boundary. **featurizer** (the split-agnostic feature engine) performs only **fit-free** imputation — zero/constant fills plus the `*_imp` flag column. **triage-pg's adapter** performs all **fit-based** imputation (mean/median/mode): the statistic is computed on the **training split only** and applied to both train and test. The reason: fit-based imputation fitted over the full `cohort × as_of_dates` matrix would leak test-period distribution into training, and only triage-pg knows the timechop train/test split — a concept featurizer must never learn.

## Consequences
- A future reader sees imputation logic in two places; this ADR records that the division is intentional and leakage-driven, not accidental.
- triage-pg's adapter must compute train-split statistics and emit the `coalesce` application for fit-based rules; featurizer needs only fit-free fills + flag columns.

## Refinement (2026-06-20) — fit-free *locus* on the SQL→Parquet path

The leakage boundary is unchanged: **fit-based imputation stays train-only, in the adapter.** What moved is only the *locus* of the (leakage-free) **fit-free** fill. triage-pg consumes featurizer via its SQL→Parquet path (`Featurizer.query`, ADR-0008 / `docs/adapter-spec.md` §2), where featurizer emits **NULL-preserving** features; featurizer's own fit-free fills live on its pandas `to_dataframe(impute=…)` path, which is off triage-pg's line. So the adapter **re-applies the fit-free fills (zero/constant + `_imp` flag) in SQL** in the same pass that applies the fit-based `COALESCE` (`docs/adapter-spec.md` §3.1, §3.5; `triage.adapters.ImputationPolicy`). Net: both fills happen in adapter SQL over `Featurizer.query`; featurizer is never asked to impute on this path, and its `measure_strategy=mean/median` must never be used (it fits over the full matrix → the very leak this ADR prevents). The fit-free/fit-based *classification* and the train-only rule are otherwise exactly as decided above.

## Extension (2026-06-21) — categorical encoding follows the same split

Categorical **encoding** is governed by the same boundary, by *vocabulary source*: a
**declared/fixed** vocabulary (config, or read from a PostgreSQL `ENUM`) is **fit-free** and
may live in featurizer; a **learned** vocabulary is a **fit-based** transform and must be fit
on the **train split only**, in the triage-pg adapter (featurizer is split-blind, ADR-0008).
The adapter's train-fit encoder mirrors the fit-based-statistic machinery exactly — category
map fit on train, persisted to `triage.matrices.metadata` (`cat_encodings`), reused for test
via the train→test parent edge, never refit. Identifiers (e.g. a facility name) are excluded
by an explicit featurizer `role`, not silent omission. Full spec: `docs/adapter-spec.md` §4.
