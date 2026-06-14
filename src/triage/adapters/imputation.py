"""Typed imputation policy for the triage-pg adapter (ADR-0009, adapter-spec §3).

Imputation is split along a leakage boundary. **Fit-free** rules (zero/constant/
null_category + the ``*_imp`` flag) compute nothing from the data and are safe anywhere.
**Fit-based** rules (mean/median/mode) compute a statistic that, per ADR-0009, MUST be
fitted on the *training split only* and applied to both train and test — only triage-pg
knows the timechop split, so this is the adapter's job and is the leakage boundary.

This module types and validates the per-metric imputation rules (the inherited
``aggregates_imputation`` / ``categoricals_imputation`` config shape) and classifies each
as fit-free / fit-based / error. It does not *apply* imputation — the adapter generates the
SQL fills at matrix-assembly time (adapter-build pass). The policy's ``canonical()`` enters
the matrix node's derivation hash, so changing it rebuilds matrices (derivation-dag §4.5).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, RootModel, model_validator

__all__ = ["ImputationRule", "ImputationPolicy", "RuleType"]

RuleType = Literal[
    "zero",
    "zero_noflag",
    "constant",
    "null_category",
    "mean",
    "median",
    "mode",
    "binary_mode",
    "error",
]

# Rules that compute a statistic from data — must be fitted on the train split only
# (ADR-0009). Everything else is fit-free (no leakage) except ``error`` (no fill at all).
_FIT_BASED: frozenset[str] = frozenset({"mean", "median", "mode", "binary_mode"})

Kind = Literal["fit_free", "fit_based", "error"]


class ImputationRule(BaseModel):
    """A single imputation rule for one feature/metric.

    ``value`` is required for — and only for — ``type: constant``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: RuleType
    value: int | float | str | None = None

    @model_validator(mode="after")
    def _check_value(self) -> ImputationRule:
        if self.type == "constant" and self.value is None:
            raise ValueError("imputation rule type 'constant' requires a 'value'")
        if self.type != "constant" and self.value is not None:
            raise ValueError(
                f"imputation rule type {self.type!r} does not take a 'value'"
            )
        return self

    @property
    def kind(self) -> Kind:
        """Classify the rule: ``fit_based`` (train-only stat), ``error``, else ``fit_free``."""
        if self.type == "error":
            return "error"
        return "fit_based" if self.type in _FIT_BASED else "fit_free"

    @property
    def fits_on_train(self) -> bool:
        """True iff this rule's statistic must be fitted on the training split (ADR-0009)."""
        return self.kind == "fit_based"

    def canonical(self) -> dict[str, Any]:
        """Deterministic, JSON-serializable form for derivation hashing."""
        out: dict[str, Any] = {"type": self.type}
        if self.value is not None:
            out["value"] = self.value
        return out


class ImputationPolicy(RootModel[dict[str, ImputationRule]]):
    """A per-metric imputation policy — the inherited ``aggregates_imputation`` shape.

    Maps a metric name (``count``, ``sum``, ``max``, …) to its rule; the reserved key
    ``all`` is the fallback applied to any metric without an explicit rule. Construct with
    ``ImputationPolicy.model_validate({"all": {"type": "zero"}, "max": {"type": "mean"}})``.
    """

    @model_validator(mode="after")
    def _non_empty(self) -> ImputationPolicy:
        if not self.root:
            raise ValueError("imputation policy must define at least one rule")
        return self

    @property
    def rules(self) -> dict[str, ImputationRule]:
        return self.root

    def resolve(self, metric: str) -> ImputationRule:
        """The rule for ``metric``: its explicit rule, else the ``all`` fallback."""
        if metric in self.root:
            return self.root[metric]
        if "all" in self.root:
            return self.root["all"]
        raise KeyError(
            f"no imputation rule for metric {metric!r} and no 'all' fallback defined"
        )

    def requires_fit(self) -> bool:
        """True iff any rule is fit-based (needs a train-split statistic, ADR-0009)."""
        return any(rule.fits_on_train for rule in self.root.values())

    def canonical(self) -> dict[str, Any]:
        """Deterministic, JSON-serializable form for derivation hashing (sorted keys)."""
        return {metric: self.root[metric].canonical() for metric in sorted(self.root)}
