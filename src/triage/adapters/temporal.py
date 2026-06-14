"""Typed ``temporal_config`` for the timechop adapter.

The validated, canonical front door to the inherited :class:`Timechop` engine
(ADR-0010, ``docs/schema-design.md`` §8.5, ``docs/adapter-spec.md`` §1). timechop stays
the as_of_date/split generator; this model only types, validates, and canonicalizes the
config that feeds it — the engine itself is unchanged.

Two design obligations beyond plain validation:

* **Deterministic canonical form** — interval fields are normalized to a single
  ``"<n> <unit>s"`` token so a config has one stable serialization regardless of surface
  spelling (``'6month'`` ≡ ``'6 months'``) or scalar-vs-list form. This is what lets
  ``temporal_config`` enter an artifact's derivation hash (``docs/derivation-dag.md`` §2).
* **Half-open date windows** — ``feature_end_time`` / ``label_end_time`` are the day
  *after* the last included date, matching the inherited timechop convention.

Validation/parsing is reused from the engine (``triage.util.conf.parse_delta_string``,
``triage.component.timechop.utils.convert_to_list``) so behavior never drifts from it.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from triage.component.timechop.utils import convert_to_list
from triage.util.conf import parse_delta_string

__all__ = ["TemporalConfig"]

# The six config fields that accept a single interval or a list of intervals.
_LIST_INTERVAL_FIELDS = (
    "training_as_of_date_frequencies",
    "test_as_of_date_frequencies",
    "max_training_histories",
    "test_durations",
    "training_label_timespans",
    "test_label_timespans",
)

# Unit normalization mirroring triage.util.conf.convert_str_to_relativedelta exactly:
# the verbose form wins, otherwise a brief abbreviation maps to its unit. Note 'm' is
# minutes (not months) — months must be spelled out — matching the engine.
_VERBOSE_UNIT = re.compile(r"^(year|month|day|week|hour|minute|second|microsecond)s?$")
_BRIEF_UNIT = {
    "y": "year",
    "d": "day",
    "w": "week",
    "h": "hour",
    "m": "minute",
    "s": "second",
    "ms": "microsecond",
}


def _canonical_interval(value: Any) -> str:
    """Normalize a Postgres-interval string to a deterministic ``"<n> <unit>s"`` token.

    Reuses the engine's ``parse_delta_string`` for the (value, unit) split (raising
    ``ValueError`` on a malformed string) and the same unit vocabulary, so
    ``convert_str_to_relativedelta(_canonical_interval(x))`` equals
    ``convert_str_to_relativedelta(x)`` for every interval the engine accepts.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"interval must be a string, got {value!r} ({type(value).__name__})"
        )
    units, magnitude = parse_delta_string(value)
    match = _VERBOSE_UNIT.match(units)
    if match:
        unit = match.group(1)
    else:
        unit = _BRIEF_UNIT.get(units.lower())
        if unit is None:
            raise ValueError(f"Unrecognized interval units {units!r} in {value!r}")
    return f"{magnitude} {unit}s"


class TemporalConfig(BaseModel):
    """A validated, canonical ``temporal_config`` for the timechop adapter.

    Field semantics are the inherited 11 timechop parameters; see
    ``docs/adapter-spec.md`` §1 for the full table. Construct from a raw config dict
    with ``TemporalConfig.model_validate(cfg)`` (or ``TemporalConfig(**cfg)``); the
    ``label_timespans`` convenience key is accepted and expands to both the training and
    test timespans. Unknown keys are rejected (``extra="forbid"``) so a typo fails loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_start_time: date
    feature_end_time: date
    label_start_time: date
    label_end_time: date
    model_update_frequency: str
    training_as_of_date_frequencies: list[str]
    test_as_of_date_frequencies: list[str]
    max_training_histories: list[str]
    test_durations: list[str]
    training_label_timespans: list[str]
    test_label_timespans: list[str]

    @model_validator(mode="before")
    @classmethod
    def _expand_label_timespans(cls, data: Any) -> Any:
        """Expand the ``label_timespans`` convenience into train + test timespans.

        Mirrors ``triage.experiments.defaults``: a single ``label_timespans`` fills both
        sides unless an explicit per-side value is already present. Popping it here (a
        ``before`` validator) keeps it from tripping ``extra="forbid"``.
        """
        if not isinstance(data, dict) or "label_timespans" not in data:
            return data
        expanded = {
            key: value for key, value in data.items() if key != "label_timespans"
        }
        shared = data["label_timespans"]
        expanded.setdefault("training_label_timespans", shared)
        expanded.setdefault("test_label_timespans", shared)
        return expanded

    @field_validator("model_update_frequency", mode="before")
    @classmethod
    def _normalize_scalar_interval(cls, value: Any) -> str:
        return _canonical_interval(value)

    @field_validator(*_LIST_INTERVAL_FIELDS, mode="before")
    @classmethod
    def _normalize_interval_list(cls, value: Any) -> list[str]:
        items = convert_to_list(value)
        if not items:
            raise ValueError("interval list must be non-empty")
        return [_canonical_interval(item) for item in items]

    @model_validator(mode="after")
    def _check_windows(self) -> TemporalConfig:
        if self.feature_start_time > self.feature_end_time:
            raise ValueError("feature_start_time is after feature_end_time")
        if self.label_start_time > self.label_end_time:
            raise ValueError("label_start_time is after label_end_time")
        return self

    def to_timechop_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for the inherited, unmodified :class:`Timechop` engine.

        Dates are emitted as ``YYYY-MM-DD`` strings (Timechop parses date strings, not
        ``date`` objects); intervals are the normalized tokens (still valid engine input).
        """
        return {
            "feature_start_time": self.feature_start_time.isoformat(),
            "feature_end_time": self.feature_end_time.isoformat(),
            "label_start_time": self.label_start_time.isoformat(),
            "label_end_time": self.label_end_time.isoformat(),
            "model_update_frequency": self.model_update_frequency,
            "training_as_of_date_frequencies": list(
                self.training_as_of_date_frequencies
            ),
            "max_training_histories": list(self.max_training_histories),
            "training_label_timespans": list(self.training_label_timespans),
            "test_as_of_date_frequencies": list(self.test_as_of_date_frequencies),
            "test_durations": list(self.test_durations),
            "test_label_timespans": list(self.test_label_timespans),
        }

    def canonical(self) -> dict[str, Any]:
        """Deterministic, JSON-serializable form for derivation hashing.

        Interval tokens are already normalized and dates are ISO strings; key order is
        irrelevant downstream because ``triage.derivation.canonical_json`` sorts keys.
        Two configs that differ only in surface form (``'6month'`` vs ``'6 months'``,
        scalar vs single-element list) produce an identical canonical dict.
        """
        return {
            "feature_start_time": self.feature_start_time.isoformat(),
            "feature_end_time": self.feature_end_time.isoformat(),
            "label_start_time": self.label_start_time.isoformat(),
            "label_end_time": self.label_end_time.isoformat(),
            "model_update_frequency": self.model_update_frequency,
            "training_as_of_date_frequencies": list(
                self.training_as_of_date_frequencies
            ),
            "test_as_of_date_frequencies": list(self.test_as_of_date_frequencies),
            "max_training_histories": list(self.max_training_histories),
            "test_durations": list(self.test_durations),
            "training_label_timespans": list(self.training_label_timespans),
            "test_label_timespans": list(self.test_label_timespans),
        }
