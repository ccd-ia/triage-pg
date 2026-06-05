# Deep Learning

**Decision:** Out of scope for triage-pg v1 (sklearn + XGBoost + regularized linear only).

**Reason:** triage-pg's problem class is tabular, temporal, entity-level data, where gradient-boosted trees and regularized linear models are state-of-the-art and deep learning reliably underperforms. DL would add GPU infrastructure, heavyweight artifacts (`.pt` files don't Parquet cleanly), and a DataLoader layer — net-new scope against a "simplify" mandate, with no current driver. It can be added later behind the same estimator interface (Parquet→numpy→`Dataset`) if a real need appears.

**Prior requests:** Raised during the 2026-06-04 design grill ("what about DL?"); confirmed out of scope.
