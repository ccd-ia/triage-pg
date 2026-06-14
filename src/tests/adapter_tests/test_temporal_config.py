"""Tests for triage.adapters.TemporalConfig (docs/adapter-spec.md §1)."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from triage.adapters import TemporalConfig
from triage.component.timechop.timechop import Timechop
from triage.derivation import canonical_json
from triage.util.conf import convert_str_to_relativedelta

# Mirrors example/config/experiment.yaml's temporal_config block.
EXAMPLE = {
    "feature_start_time": "1995-01-01",
    "feature_end_time": "2015-01-01",
    "label_start_time": "2012-01-01",
    "label_end_time": "2015-01-01",
    "model_update_frequency": "6month",
    "training_as_of_date_frequencies": "1day",
    "test_as_of_date_frequencies": "3month",
    "max_training_histories": ["6month", "3month"],
    "test_durations": ["0day", "1month", "2month"],
    "training_label_timespans": ["1month"],
    "test_label_timespans": ["7day"],
}

_EXAMPLE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "example" / "config" / "experiment.yaml"
)


def _temporal_signature(splits):
    """The datetime-only structure of chop_time() output.

    Ignores the interval-string metadata (which echoes the raw input verbatim), so two
    runs fed semantically-equal-but-differently-spelled intervals can be compared on the
    temporal structure that actually matters.
    """
    signature = []
    for split in splits:
        train = split["train_matrix"]
        tests = [
            (
                test["first_as_of_time"],
                test["last_as_of_time"],
                test["matrix_info_end_time"],
                list(test["as_of_times"]),
            )
            for test in split["test_matrices"]
        ]
        signature.append(
            (
                split["feature_start_time"],
                split["feature_end_time"],
                split["label_start_time"],
                split["label_end_time"],
                train["first_as_of_time"],
                train["last_as_of_time"],
                train["matrix_info_end_time"],
                list(train["as_of_times"]),
                tests,
            )
        )
    return signature


def test_parses_example_config():
    tc = TemporalConfig.model_validate(EXAMPLE)
    assert tc.feature_start_time.isoformat() == "1995-01-01"
    assert tc.label_end_time.isoformat() == "2015-01-01"
    # intervals normalized to canonical "<n> <unit>s" tokens
    # canonical tokens are consistently plural ("<n> <unit>s")
    assert tc.model_update_frequency == "6 months"
    assert tc.max_training_histories == ["6 months", "3 months"]
    assert tc.test_durations == ["0 days", "1 months", "2 months"]


def test_scalar_interval_coerced_to_list():
    tc = TemporalConfig.model_validate(EXAMPLE)
    # given as the bare string '1day' / '3month'
    assert tc.training_as_of_date_frequencies == ["1 days"]
    assert tc.test_as_of_date_frequencies == ["3 months"]


def test_label_timespans_convenience_expands():
    cfg = {key: value for key, value in EXAMPLE.items()}
    del cfg["training_label_timespans"]
    del cfg["test_label_timespans"]
    cfg["label_timespans"] = ["7day"]
    tc = TemporalConfig.model_validate(cfg)
    assert tc.training_label_timespans == ["7 days"]
    assert tc.test_label_timespans == ["7 days"]


def test_explicit_timespans_win_over_convenience():
    cfg = {key: value for key, value in EXAMPLE.items()}
    cfg["label_timespans"] = ["99day"]  # ignored because explicit ones are present
    tc = TemporalConfig.model_validate(cfg)
    assert tc.training_label_timespans == ["1 months"]
    assert tc.test_label_timespans == ["7 days"]


def test_invalid_interval_rejected():
    cfg = {key: value for key, value in EXAMPLE.items()}
    cfg["model_update_frequency"] = "every other tuesday"
    with pytest.raises(ValidationError):
        TemporalConfig.model_validate(cfg)


def test_empty_interval_list_rejected():
    cfg = {key: value for key, value in EXAMPLE.items()}
    cfg["test_durations"] = []
    with pytest.raises(ValidationError):
        TemporalConfig.model_validate(cfg)


def test_reversed_feature_window_rejected():
    cfg = {key: value for key, value in EXAMPLE.items()}
    cfg["feature_start_time"] = "2016-01-01"  # after feature_end_time
    with pytest.raises(ValidationError):
        TemporalConfig.model_validate(cfg)


def test_reversed_label_window_rejected():
    cfg = {key: value for key, value in EXAMPLE.items()}
    cfg["label_start_time"] = "2099-01-01"  # after label_end_time
    with pytest.raises(ValidationError):
        TemporalConfig.model_validate(cfg)


def test_unknown_key_rejected():
    cfg = {key: value for key, value in EXAMPLE.items()}
    cfg["feature_star_time"] = "1995-01-01"  # typo
    with pytest.raises(ValidationError):
        TemporalConfig.model_validate(cfg)


def test_canonical_is_deterministic_across_surface_forms():
    scalar_form = {
        **EXAMPLE,
        "training_as_of_date_frequencies": "1day",
        "model_update_frequency": "6month",
    }
    list_and_verbose_form = {
        # same logical config, different surface: 1-elem list + verbose spelling +
        # reordered keys (dict order must not matter)
        "test_label_timespans": ["7 days"],
        "training_label_timespans": ["1 month"],
        "test_durations": ["0 day", "1 month", "2 month"],
        "max_training_histories": ["6 months", "3 months"],
        "test_as_of_date_frequencies": "3 months",
        "training_as_of_date_frequencies": ["1 day"],
        "model_update_frequency": "6 months",
        "label_end_time": "2015-01-01",
        "label_start_time": "2012-01-01",
        "feature_end_time": "2015-01-01",
        "feature_start_time": "1995-01-01",
    }
    a = TemporalConfig.model_validate(scalar_form).canonical()
    b = TemporalConfig.model_validate(list_and_verbose_form).canonical()
    assert a == b
    assert canonical_json(a) == canonical_json(b)


def test_canonical_interval_preserves_engine_semantics():
    # canonicalizing must not change what the engine parses the interval to
    for raw in ["6month", "6 months", "1day", "0day", "2y", "1 week", "3month"]:
        tc = TemporalConfig.model_validate({**EXAMPLE, "model_update_frequency": raw})
        canonical_token = tc.model_update_frequency
        assert convert_str_to_relativedelta(canonical_token) == (
            convert_str_to_relativedelta(raw)
        )


def test_to_timechop_kwargs_feeds_engine():
    tc = TemporalConfig.model_validate(EXAMPLE)
    splits = Timechop(**tc.to_timechop_kwargs()).chop_time()
    assert splits, "expected at least one train/test split"
    for split in splits:
        assert list(split["train_matrix"]["as_of_times"])
        assert split["test_matrices"]
        for test in split["test_matrices"]:
            assert list(test["as_of_times"])


def test_canonical_intervals_yield_same_splits_as_raw():
    # feeding canonical tokens must reproduce the same temporal structure as the raw
    # strings the inherited engine would have received directly
    raw_kwargs = {
        "feature_start_time": "1995-01-01",
        "feature_end_time": "2015-01-01",
        "label_start_time": "2012-01-01",
        "label_end_time": "2015-01-01",
        "model_update_frequency": "6month",
        "training_as_of_date_frequencies": "1day",
        "max_training_histories": ["6month", "3month"],
        "training_label_timespans": ["1month"],
        "test_as_of_date_frequencies": "3month",
        "test_durations": ["0day", "1month", "2month"],
        "test_label_timespans": ["7day"],
    }
    raw_splits = Timechop(**raw_kwargs).chop_time()
    canonical_splits = Timechop(
        **TemporalConfig.model_validate(EXAMPLE).to_timechop_kwargs()
    ).chop_time()
    assert _temporal_signature(raw_splits) == _temporal_signature(canonical_splits)


def test_example_config_file_still_valid():
    # guards the shipped example against drift from the model
    with open(_EXAMPLE_CONFIG_PATH) as handle:
        config = yaml.safe_load(handle)
    tc = TemporalConfig.model_validate(config["temporal_config"])
    assert Timechop(**tc.to_timechop_kwargs()).chop_time()
