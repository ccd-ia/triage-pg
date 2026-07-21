# 0030. Target-history point-in-time path (two shapes, one leakage boundary)

- Status: Accepted
- Date: 2026-07-20
- Deciders: Adolfo (scope), Claude (recommendation per the v1.0.1 baselines plan)

The v1.0.1 time-series baselines (persistence, moving-average, drift, promedio, seasonal-naive,
Holt–Winters/ETS, Croston/SBA) need each entity's **own prior target values**. The cross-sectional
design matrix carries only features plus one forward label per `(entity_id, as_of_date)`, so that
history is absent by design. Exposing it is a genuine point-in-time boundary — the label-side
analog of the fit-free/fit-based imputation split (ADR-0009) — and it is hard to reverse: it fixes
the shape every baseline estimator and the derivation-DAG identity (ADRs 0013–0017) will consume.

## Decision

Expose per-entity prior target history in **two shapes** under **one** leakage boundary, both
excluded from the feature set and from fit-based imputation (ADR-0009), both first-class
derivation-DAG artifacts:

1. **Windowed-label lags** — reserved `_target_lag_*` columns joined into the design matrix,
   built in `adapters/target_history.py` by reusing `labels._label_projection` at prior
   as_of_dates. A label realized at date `t` over horizon `w` is admissible only where
   `t + w ≤ as_of_date` — its window must have fully elapsed to be knowable. Consumed by the
   cheap baselines (persistence/MA/drift/promedio); requires no new config.
2. **Raw periodic series** — a sidecar keyed by `(entity_id, as_of_date)`, built from a new
   *optional* `history_query` config key (a period-level aggregation with its own knowledge-date
   discipline: only rows with `knowledge_date < as_of_date`). Handed to the estimator alongside
   X as a variable-length series, not matrix columns. Consumed by seasonal-naive / Holt–Winters /
   ETS / Croston; a raw-series baseline in the grid with no `history_query` is a config error.

The two shapes stay separate rather than converging on one query: the raw periodic series
(period-level counts) is a genuinely different quantity than the forward windowed label, so it
earns its own explicit, PIT-managed `history_query`. The lag path reuses the label query because
those lags *are* the same quantity, just realized at earlier as_of_dates — inventing a second
query for it would duplicate `_label_projection` for no PIT benefit.

## Consequences

- New optional config surface (`history_query`) and a new module (`adapters/target_history.py`);
  both `_target_lag_*` columns and the raw-series sidecar are excluded from the feature list,
  categorical encoding, and both imputation passes — no real model can train on the label's own
  lags, and structurally-missing history is never corrupted by an imputation fill.
- Two boundaries become invariants enforced by leak tests: `t + w ≤ as_of_date` for lags,
  `knowledge_date < as_of_date` for the raw series (ADR-0010's ranking spine and ADR-0009's
  leakage discipline extend cleanly to this label-side case).
- Written before its consumers (the Phase 4 baseline estimators) so the shape does not calcify
  leakily; a red leak test is a hard stop on any baseline work downstream of this ADR.
