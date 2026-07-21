"""Trivial cross-sectional floors — sklearn ``Dummy*`` baselines (v1.0.1, Phase 1).

These are the classification + regression metric floors that need NO new data path:
a ``DummyClassifier`` predicts a constant class prior; a ``DummyRegressor`` predicts a
constant statistic of the training target. Both flow through the existing estimator seam
(:func:`triage.adapters.model._score_column`) unchanged, and — crucially — the ADR-0011
feature-importance persistence must tolerate them (they expose neither
``feature_importances_`` nor ``coef_``).

No production code is added for this floor: the score contract, the importance path, and
the constructor-arg handling already cover the ``Dummy*`` estimators. This file is the
proof of that.
"""

import numpy as np
import pytest
from sklearn.dummy import DummyClassifier, DummyRegressor

from triage.adapters.model import (
    _feature_importance_values,
    _import_estimator,
    _instantiate,
    _score_column,
)

# A tiny 2-feature design matrix (rows = entities).
X = np.array([[0.0, 1.0], [1.0, 0.0], [2.0, 1.0], [3.0, 0.0]])


def test_dummy_classifier_score_is_constant_positive_proba():
    """``_score_column`` takes the positive-class probability; for ``strategy='prior'``
    that is the constant base rate P(y=1)."""
    y = np.array([0, 1, 1, 0])
    clf = DummyClassifier(strategy="prior").fit(X, y)

    scores = _score_column(clf, X)

    assert scores.shape == (4,)
    assert np.allclose(scores, scores[0])  # constant → mass ties (D6)
    assert scores[0] == pytest.approx(0.5)  # P(y=1) = 2/4


def test_dummy_regressor_score_is_constant_prediction():
    """No ``predict_proba``/``decision_function`` → ``_score_column`` falls to
    ``predict``; ``strategy='mean'`` predicts the constant training mean."""
    y = np.array([10.0, 20.0, 30.0, 40.0])
    reg = DummyRegressor(strategy="mean").fit(X, y)

    scores = _score_column(reg, X)

    assert scores.shape == (4,)
    assert np.allclose(scores, 25.0)  # mean([10,20,30,40])


def test_dummy_estimators_expose_no_feature_importances():
    """ADR-0011 importance persistence must not crash on estimators with neither
    ``feature_importances_`` nor ``coef_`` — it returns ``None`` (nothing persisted)."""
    clf = DummyClassifier(strategy="most_frequent").fit(X, np.array([0, 1, 1, 0]))
    reg = DummyRegressor(strategy="median").fit(X, np.array([1.0, 2.0, 3.0, 4.0]))

    assert _feature_importance_values(clf, n_features=2) is None
    assert _feature_importance_values(reg, n_features=2) is None


def test_instantiate_dummy_regressor_without_random_state():
    """``DummyRegressor`` has no ``random_state`` param; ``_instantiate`` must construct
    it without injecting the seed (ADR-0016 seeding applies only where accepted). The
    quantile strategy is the one pinball@τ (Phase 5) scores."""
    cls = _import_estimator("sklearn.dummy.DummyRegressor")
    est = _instantiate(cls, {"strategy": "quantile", "quantile": 0.9}, random_seed=42)

    y = np.array([1.0, 2.0, 3.0, 4.0])
    est.fit(X, y)
    scores = _score_column(est, X)

    assert np.allclose(scores, np.quantile(y, 0.9))
