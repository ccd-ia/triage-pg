"""Survival estimator wrappers (ADR-0026) — the survival analog of ScaledLogisticRegression.

scikit-survival's ``CoxPHSurvivalAnalysis`` runs unregularized-scale Newton-Raphson: with
features on wildly different scales (0/1 one-hots next to backlog COUNTs in the thousands, the
exact geometry featurizer emits), ``exp(Xβ)`` overflows during the first steps → NaN Hessian →
a LAPACK "illegal value" crash. The house pattern for this is
:class:`~triage.component.catwalk.estimators.classifiers.ScaledLogisticRegression` — bundle a
MinMaxScaler in front so every feature lands on [0, 1] — and this module applies it to Cox.
A side benefit is identical to the LR case: the persisted coefficients (ADR-0011) are
comparable across features, and ``exp(β)`` reads as a hazard ratio per feature-range.
"""

from sklearn.base import BaseEstimator
from sklearn.preprocessing import MinMaxScaler


class ScaledCoxPHSurvivalAnalysis(BaseEstimator):
    """MinMax-scaled Cox proportional hazards (scikit-survival).

    Fits on the structured survival ``y`` (``Surv.from_arrays(event, duration)`` — the
    adapter's survival fit branch builds it); ``predict`` returns the RISK score (higher =
    event sooner), which is what the ranking spine stores (ADR-0010). ``coef_`` is exposed so
    the train-time importance persistence (ADR-0011) records signed log-hazard coefficients.
    """

    # The adapter's fit-branch marker: this estimator consumes the structured survival label
    # pair even though its module is not `sksurv.*` (see adapters.model._is_survival_estimator).
    is_survival_estimator = True

    def __init__(self, alpha=0.1, ties="breslow", n_iter=100, tol=1e-9):
        self.alpha = alpha
        self.ties = ties
        self.n_iter = n_iter
        self.tol = tol
        self.minmax_scaler = MinMaxScaler()

    def fit(self, x, y):
        from sksurv.linear_model import CoxPHSurvivalAnalysis

        self.model = CoxPHSurvivalAnalysis(
            alpha=self.alpha, ties=self.ties, n_iter=self.n_iter, tol=self.tol
        )
        x = self.minmax_scaler.fit_transform(x)
        self.model.fit(x, y)
        self.coef_ = self.model.coef_
        return self

    def predict(self, x):
        return self.model.predict(self.minmax_scaler.transform(x))
