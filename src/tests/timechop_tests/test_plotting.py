from unittest import TestCase
from unittest.mock import patch

import matplotlib

matplotlib.use("Agg")
from triage.component.timechop import Timechop  # noqa
from triage.component.timechop.plotting import visualize_chops  # noqa

# A known-good temporal_config in the exact Timechop constructor shape (the greenfield
# adapter's TemporalConfig.to_timechop_kwargs() emits this same set of keys). Inlined so the
# plotting smoke test carries no dependency on any example config file.
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


class VisualizeChopTest(TestCase):
    @property
    def chopper(self):
        return Timechop(**TEMPORAL_CONFIG)

    # hard to make many assertions, but we can make sure it gets to the end
    # and shows the contents.

    # we do one such test case to work out each combination of boolean arguments
    def test_default_args(self):
        with patch("triage.component.timechop.plotting.plt.show") as show_patch:
            visualize_chops(self.chopper)
            assert show_patch.called

    def test_no_as_of_times(self):
        with patch("triage.component.timechop.plotting.plt.show") as show_patch:
            visualize_chops(self.chopper, show_as_of_times=False)
            assert show_patch.called

    def test_no_boundaries(self):
        with patch("triage.component.timechop.plotting.plt.show") as show_patch:
            visualize_chops(self.chopper, show_boundaries=False)
            assert show_patch.called

    def test_no_boundaries_or_as_of_times(self):
        with patch("triage.component.timechop.plotting.plt.show") as show_patch:
            visualize_chops(self.chopper, show_as_of_times=False, show_boundaries=False)
            assert show_patch.called
