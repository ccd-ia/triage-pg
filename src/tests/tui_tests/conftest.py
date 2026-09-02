"""Fixtures for the TUI adapters: the dashboard's seeded experiment over a lynkeus source.

The seed (two runs sharing one experiment, the second cache-sharing the
first's models; cohort/labels/model artifacts; evaluations; predictions; a
refreshed leaderboard) is the same one the read-dashboard contract tests use,
so the adapters are exercised against the same shape the dashboard renders.
"""

from __future__ import annotations

import pytest
from lynkeus import PgSource

from tests.dashboard_tests.conftest import SeededExperiment, _seed_full_experiment
from triage.util.db import libpq_conninfo

pytest_plugins = ["lynkeus.testing"]


@pytest.fixture
def seeded(db_pool_greenfield) -> SeededExperiment:
    return _seed_full_experiment(db_pool_greenfield)


@pytest.fixture
def source(db_url, seeded) -> PgSource:
    """A lynkeus source over the seeded throwaway DB."""
    return PgSource(dsn=libpq_conninfo(db_url))
