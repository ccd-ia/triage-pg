"""Smoke tests for the temporal cross-validation viz (matplotlib + plotly backends)."""

from unittest.mock import patch

import matplotlib

matplotlib.use("Agg")

from triage.component.timechop import Timechop  # noqa: E402
from triage.component.timechop.plotting import (  # noqa: E402
    visualize_chops,
    visualize_chops_plotly,
)

# A known-good temporal_config in the exact Timechop constructor shape (the greenfield adapter's
# TemporalConfig.to_timechop_kwargs() emits this same key set). Inlined so the test carries no
# dependency on an example config file.
TEMPORAL_CONFIG = {
    "feature_start_time": "2014-01-01",
    "feature_end_time": "2017-07-01",
    "label_start_time": "2015-01-01",
    "label_end_time": "2017-07-01",
    "model_update_frequency": "6month",
    "training_as_of_date_frequencies": "6month",
    "max_training_histories": "5year",
    "training_label_timespans": ["6month"],
    "test_as_of_date_frequencies": "6month",
    "test_durations": "0day",
    "test_label_timespans": ["6month"],
}


def _chopper():
    return Timechop(**TEMPORAL_CONFIG)


def test_visualize_chops_interactive_calls_show():
    """With no save_target the interactive path calls plt.show()."""
    with patch("triage.component.timechop.plotting.plt.show") as show_patch:
        visualize_chops(_chopper())
        assert show_patch.called


def test_visualize_chops_saves_image(tmp_path):
    """A save_target writes a non-empty image and does NOT pop an interactive window."""
    out = tmp_path / "blocks.png"
    with patch("triage.component.timechop.plotting.plt.show") as show_patch:
        visualize_chops(_chopper(), save_target=str(out))
        assert not show_patch.called
    assert out.exists() and out.stat().st_size > 0


def test_visualize_chops_without_label_windows(tmp_path):
    """The show_label_windows toggle still renders."""
    out = tmp_path / "blocks_nolabels.png"
    visualize_chops(_chopper(), save_target=str(out), show_label_windows=False)
    assert out.exists() and out.stat().st_size > 0


def test_visualize_chops_plotly_writes_selfcontained_html(tmp_path):
    """The interactive backend writes a self-contained HTML page (plotly.js embedded)."""
    out = tmp_path / "blocks.html"
    visualize_chops_plotly(_chopper(), save_target=str(out))
    assert out.exists()
    # include_plotlyjs=True embeds the ~MB plotly bundle: a large file proves self-containment.
    assert out.stat().st_size > 100_000
