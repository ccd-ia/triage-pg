"""Postmodeling diagnostics (plan P5) — the ADR-0011 shape, made concrete.

Matrices live in Parquet (local FS or S3), not in PostgreSQL, so the two diagnostics
that need feature VALUES follow one pattern: a ``triage postmodel`` CLI command
computes from the matrix ONCE, persists long-format rows into ``triage.crosstabs`` /
``triage.error_analysis`` (migration 0017), and the dashboard + CLI read those tables.
No standalone postmodeling module returns (ADR-0011); this package is the compute
half of two persisted surfaces.
"""

from triage.diagnostics.crosstabs import compute_crosstabs
from triage.diagnostics.error_tree import compute_error_analysis

__all__ = ["compute_crosstabs", "compute_error_analysis"]
