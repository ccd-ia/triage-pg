"""Marginal survival baselines (v1.0.1, Phase 2).

The three covariate-free floors predict a constant per-entity risk (→ in-PG C-index ≈ 0.5, the
survival floor); ``SingleFeatureCox`` discriminates on one covariate. scikit-survival lives in
the optional ``survival`` extra, so the whole module is skipped when it is not installed.
"""

import numpy as np
import pytest

pytest.importorskip("sksurv")

from sksurv.util import Surv  # noqa: E402

from triage.adapters.model import (  # noqa: E402
    _feature_importance_values,
    _score_column,
)
from triage.component.catwalk.baselines.survival import (  # noqa: E402
    KaplanMeierBaseline,
    MarginalHazardBaseline,
    NelsonAalenBaseline,
    SingleFeatureCox,
)

# 6 entities. Feature column 0 is monotonically HIGHER for shorter-time subjects, so a Cox on
# it should rank shorter survivors as higher-risk. Two subjects are censored (event=False).
X = np.array([[5.0, 1.0], [4.0, 0.0], [3.0, 1.0], [2.0, 0.0], [1.0, 1.0], [0.0, 0.0]])
EVENT = np.array([True, True, True, False, True, False])
TIME = np.array([1.0, 2.0, 3.0, 5.0, 6.0, 9.0])
Y = Surv.from_arrays(event=EVENT, time=TIME)


@pytest.mark.parametrize(
    "cls", [KaplanMeierBaseline, NelsonAalenBaseline, MarginalHazardBaseline]
)
def test_marginal_baselines_predict_constant_risk(cls):
    """Every marginal baseline broadcasts one population risk → an all-tied ranking."""
    est = cls().fit(X, Y)
    risk = est.predict(X)

    assert risk.shape == (6,)
    assert np.allclose(risk, risk[0])  # constant → C-index ≈ 0.5 floor
    assert np.isfinite(risk[0])


def test_base_rate_equals_event_fraction():
    est = MarginalHazardBaseline().fit(X, Y)
    assert est.predict(X)[0] == pytest.approx(EVENT.mean())  # 4/6 observed events


def test_marginal_baselines_expose_no_importances():
    """ADR-0011: a marginal baseline exposes neither ``feature_importances_`` nor ``coef_``."""
    est = KaplanMeierBaseline().fit(X, Y)
    assert _feature_importance_values(est, n_features=2) is None


def test_score_column_uses_predict_for_survival_baselines():
    """No ``predict_proba``/``decision_function`` → the score is ``predict`` (the risk)."""
    est = NelsonAalenBaseline().fit(X, Y)
    scores = _score_column(est, X)

    assert scores.shape == (6,)
    assert np.allclose(scores, scores[0])


def test_single_feature_cox_discriminates():
    """Cox on the single anti-correlated covariate ranks shorter survivors as higher-risk."""
    est = SingleFeatureCox(feature_index=0).fit(X, Y)
    risk = est.predict(X)

    assert risk.shape == (6,)
    # entity 0 (x=5, t=1) must out-risk entity 5 (x=0, t=9).
    assert risk[0] > risk[5]
