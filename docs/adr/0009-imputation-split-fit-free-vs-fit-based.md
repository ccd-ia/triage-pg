# 0009. Imputation split: fit-free in featurizer, fit-based in triage-pg

- Status: Accepted
- Date: 2026-06-04

Imputation is deliberately split across the two repos along a leakage boundary. **featurizer** (the split-agnostic feature engine) performs only **fit-free** imputation — zero/constant fills plus the `*_imp` flag column. **triage-pg's adapter** performs all **fit-based** imputation (mean/median/mode): the statistic is computed on the **training split only** and applied to both train and test. The reason: fit-based imputation fitted over the full `cohort × as_of_dates` matrix would leak test-period distribution into training, and only triage-pg knows the timechop train/test split — a concept featurizer must never learn.

## Consequences
- A future reader sees imputation logic in two places; this ADR records that the division is intentional and leakage-driven, not accidental.
- triage-pg's adapter must compute train-split statistics and emit the `coalesce` application for fit-based rules; featurizer needs only fit-free fills + flag columns.
