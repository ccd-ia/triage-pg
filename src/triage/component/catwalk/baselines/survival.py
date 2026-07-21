"""Marginal survival baselines (v1.0.1, Phase 2) — the survival analog of the classification
metric floors.

Each is a survival estimator (ADR-0010/0026): it declares ``is_survival_estimator = True``,
fits on the structured ``(event, time)`` label the adapter builds with ``Surv.from_arrays``
(see :func:`triage.adapters.model._fit_survival_estimator`), and its ``predict`` returns a RISK
score (higher = event sooner) that the ranking spine stores in ``predictions.score``.

The three MARGINAL baselines (Kaplan–Meier, Nelson–Aalen, base-rate) assign every entity the
SAME risk, so they carry no discriminative power — the in-PG C-index sits at ≈ 0.5, the floor a
real survival model must clear. :class:`SingleFeatureCox` is the survival analog of
rank-by-one-feature (``LinearRanker``): a Cox model on a single covariate.

scikit-survival is already a dependency (the ``survival`` extra); these add no new dep. Because
these expose neither ``feature_importances_`` nor ``coef_``, the ADR-0011 importance
persistence skips them cleanly (``_feature_importance_values`` returns ``None``).
"""

# _risk / _cox / _col are fitted attributes set in fit() (the sklearn convention), never
# __init__ — turn off the rule that flags exactly that pattern.
# pyright: reportUninitializedInstanceVariable=false
from __future__ import annotations

import numpy as np
from sklearn.base import BaseEstimator


def _split_structured_y(y):
    """Return ``(event: bool array, time: float array)`` from the sksurv structured survival y.

    The adapter builds ``y`` with ``Surv.from_arrays(event=..., time=...)`` (ADR-0026), whose
    default field names are ``('event', 'time')``; read them positionally so this is robust to
    the exact names.
    """
    names = y.dtype.names
    event = np.asarray(y[names[0]]).astype(bool)
    time = np.asarray(y[names[1]]).astype(float)
    return event, time


class _MarginalSurvivalBaseline(BaseEstimator):
    """Base for the covariate-free floors: ``fit`` ignores X and stores one population risk;
    ``predict`` broadcasts it to every row (a degenerate, all-tied ranking → C-index ≈ 0.5).
    """

    is_survival_estimator = True
    _risk: float = 0.0

    def predict(self, x):
        n = np.asarray(x).shape[0]
        return np.full(n, self._risk, dtype=float)


class KaplanMeierBaseline(_MarginalSurvivalBaseline):
    """Population Kaplan–Meier survival curve. Risk = cumulative incidence ``1 − S(t*)`` at the
    last estimated time point — one population number broadcast to every entity."""

    def fit(self, x, y):
        from sksurv.nonparametric import kaplan_meier_estimator

        event, time = _split_structured_y(y)
        # sksurv may return (time, surv) or (time, surv, conf_int); take the survival array.
        surv = kaplan_meier_estimator(event, time)[1]
        self._risk = float(1.0 - surv[-1]) if len(surv) else 0.0
        return self


class NelsonAalenBaseline(_MarginalSurvivalBaseline):
    """Population Nelson–Aalen cumulative hazard. Risk = ``H(t*)`` at the last event time."""

    def fit(self, x, y):
        from sksurv.nonparametric import nelson_aalen_estimator

        event, time = _split_structured_y(y)
        chf = nelson_aalen_estimator(event, time)[1]
        self._risk = float(chf[-1]) if len(chf) else 0.0
        return self


class MarginalHazardBaseline(_MarginalSurvivalBaseline):
    """Base-rate floor: risk = the marginal event fraction (share of observed events)."""

    def fit(self, x, y):
        event, _ = _split_structured_y(y)
        self._risk = float(event.mean()) if event.size else 0.0
        return self


class SingleFeatureCox(BaseEstimator):
    """Cox proportional hazards on a SINGLE covariate — the survival analog of
    rank-by-one-feature (``LinearRanker``).

    Selects column ``feature_index`` from the design matrix (the adapter hands the estimator a
    plain numpy matrix, so selection is positional), then fits the house
    :class:`~triage.component.catwalk.estimators.survival.ScaledCoxPHSurvivalAnalysis` (MinMax-
    scaled to avoid the ``exp(Xβ)`` overflow, ADR-0026). ``predict`` returns the risk score.
    """

    is_survival_estimator = True

    def __init__(self, feature_index=0, alpha=0.1):
        self.feature_index = feature_index
        self.alpha = alpha

    def fit(self, x, y):
        from triage.component.catwalk.estimators.survival import (
            ScaledCoxPHSurvivalAnalysis,
        )

        col = np.asarray(x)[:, [self.feature_index]]
        self._cox = ScaledCoxPHSurvivalAnalysis(alpha=self.alpha)
        self._cox.fit(col, y)
        return self

    def predict(self, x):
        col = np.asarray(x)[:, [self.feature_index]]
        return self._cox.predict(col)
