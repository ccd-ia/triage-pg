"""triage-pg adapters — the glue mapping triage concepts onto featurizer/storage/auth.

The adapters own the triage-pg ↔ featurizer seam: timechop splits → featurizer
``as_of_dates``, cohort, labels, matrix assembly, cache keys, and fit-based imputation.
This package is the first piece of the adapter-spec pass; see ``docs/adapter-spec.md``.
"""

from triage.adapters.cohort import build_cohort
from triage.adapters.imputation import ImputationPolicy, ImputationRule
from triage.adapters.labels import build_labels
from triage.adapters.matrix import MatrixResult, build_matrix
from triage.adapters.model import (
    ModelResult,
    ScoreEvaluateResult,
    build_model,
    score_and_evaluate,
)
from triage.adapters.temporal import TemporalConfig

__all__ = [
    "ImputationPolicy",
    "ImputationRule",
    "MatrixResult",
    "ModelResult",
    "ScoreEvaluateResult",
    "TemporalConfig",
    "build_cohort",
    "build_labels",
    "build_matrix",
    "build_model",
    "score_and_evaluate",
]
