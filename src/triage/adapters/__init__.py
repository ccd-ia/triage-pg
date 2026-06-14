"""triage-pg adapters — the glue mapping triage concepts onto featurizer/storage/auth.

The adapters own the triage-pg ↔ featurizer seam: timechop splits → featurizer
``as_of_dates``, cohort, labels, matrix assembly, cache keys, and fit-based imputation.
This package is the first piece of the adapter-spec pass; see ``docs/adapter-spec.md``.
"""

from triage.adapters.temporal import TemporalConfig

__all__ = ["TemporalConfig"]
