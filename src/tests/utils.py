import functools
import importlib
import os
import random
from contextlib import contextmanager
from functools import cached_property
from unittest import mock

import matplotlib
import numpy as np
import pandas as pd
from sqlalchemy import text

# CONFIG_VERSION was historically imported from triage.experiments, which has
# been removed in the greenfield cutover. The value is inlined here so the
# remaining (greenfield) tests that build sample configs keep working.
CONFIG_VERSION = "v8"

matplotlib.use("Agg")

from matplotlib import pyplot as plt  # noqa

CONFIG_QUERY_DATA = {
    "cohort": {
        "query": """
            select distinct(entity_id)
            from events
            where '{as_of_date}'::date >= outcome_date
        """,
        "filepath": "cohorts/file.sql",
    },
    "label": {
        "query": """
            select
                events.entity_id,
                bool_or(outcome::bool)::integer as outcome
            from events
            where '{as_of_date}'::date <= outcome_date
                and outcome_date < '{as_of_date}'::date + interval '{label_timespan}'
            group by entity_id
        """,
        "filepath": "labels/file.sql",
    },
}

MOCK_FILES = {
    os.path.join(
        os.path.abspath(os.getcwd()), f"{CONFIG_QUERY_DATA['label']['filepath']}"
    ): CONFIG_QUERY_DATA["label"]["query"],
    os.path.join(
        os.path.abspath(os.getcwd()), f"{CONFIG_QUERY_DATA['cohort']['filepath']}"
    ): CONFIG_QUERY_DATA["cohort"]["query"],
}


def open_side_effect(name):
    return mock.mock_open(read_data=MOCK_FILES[name]).return_value


def fake_labels(length):
    return np.array([random.choice([True, False]) for i in range(0, length)])


def matrix_creator():
    """Return a sample matrix."""

    source_dict = {
        "entity_id": [1, 2],
        "as_of_date": [pd.Timestamp(2016, 1, 1), pd.Timestamp(2016, 1, 1)],
        "feature_one": [3, 4],
        "feature_two": [5, 6],
        "label": [0, 1],
    }
    return pd.DataFrame.from_dict(source_dict)


def populate_source_data(db_engine):
    complaints = [
        (1, "2010-10-01", 5),
        (1, "2011-10-01", 4),
        (1, "2011-11-01", 4),
        (1, "2011-12-01", 4),
        (1, "2012-02-01", 5),
        (1, "2012-10-01", 4),
        (1, "2013-10-01", 5),
        (2, "2010-10-01", 5),
        (2, "2011-10-01", 5),
        (2, "2011-11-01", 4),
        (2, "2011-12-01", 4),
        (2, "2012-02-01", 6),
        (2, "2012-10-01", 5),
        (2, "2013-10-01", 6),
        (3, "2010-10-01", 5),
        (3, "2011-10-01", 3),
        (3, "2011-11-01", 4),
        (3, "2011-12-01", 4),
        (3, "2012-02-01", 4),
        (3, "2012-10-01", 3),
        (3, "2013-10-01", 4),
    ]

    entity_zip_codes = [(1, "60120"), (2, "60123"), (3, "60123")]

    zip_code_demographics = [
        ("60120", "hispanic", "2011-01-01"),
        ("60123", "white", "2011-01-01"),
    ]

    zip_code_events = [("60120", "2012-10-01", 1), ("60123", "2012-10-01", 10)]

    events = [
        (1, 1, "2011-01-01"),
        (1, 1, "2011-06-01"),
        (1, 1, "2011-09-01"),
        (1, 1, "2012-01-01"),
        (1, 1, "2012-01-10"),
        (1, 1, "2012-06-01"),
        (1, 1, "2013-01-01"),
        (1, 0, "2014-01-01"),
        (1, 1, "2015-01-01"),
        (2, 1, "2011-01-01"),
        (2, 1, "2011-06-01"),
        (2, 1, "2011-09-01"),
        (2, 1, "2012-01-01"),
        (2, 1, "2013-01-01"),
        (2, 1, "2014-01-01"),
        (2, 1, "2015-01-01"),
        (3, 0, "2011-01-01"),
        (3, 0, "2011-06-01"),
        (3, 0, "2011-09-01"),
        (3, 0, "2012-01-01"),
        (3, 0, "2013-01-01"),
        (3, 1, "2014-01-01"),
        (3, 0, "2015-01-01"),
    ]

    with db_engine.begin() as conn:
        conn.execute(text("""
                create table cat_complaints (
                entity_id int,
                as_of_date date,
                cat_sightings int
                )
                """))

        conn.execute(text("""
                create table entity_zip_codes (
                entity_id int,
                zip_code text
                )
                """))

        conn.execute(
            text(
                "create table zip_code_demographics (zip_code text, ethnicity text, as_of_date date)"
            )
        )
        for demographic_row in zip_code_demographics:
            conn.execute(
                text(
                    "insert into zip_code_demographics values (:zip_code, :ethnicity, :as_of_date)"
                ),
                {
                    "zip_code": demographic_row[0],
                    "ethnicity": demographic_row[1],
                    "as_of_date": demographic_row[2],
                },
            )

        for entity_zip_code in entity_zip_codes:
            conn.execute(
                text("insert into entity_zip_codes values (:entity_id, :zip_code)"),
                {
                    "entity_id": entity_zip_code[0],
                    "zip_code": entity_zip_code[1],
                },
            )

        conn.execute(text("""
                create table zip_code_events (
                zip_code text,
                as_of_date date,
                num_events int
                )
                """))
        for zip_code_event in zip_code_events:
            conn.execute(
                text(
                    "insert into zip_code_events values (:zip_code, :as_of_date, :num_events)"
                ),
                {
                    "zip_code": zip_code_event[0],
                    "as_of_date": zip_code_event[1],
                    "num_events": zip_code_event[2],
                },
            )

        for complaint in complaints:
            conn.execute(
                text(
                    "insert into cat_complaints values (:entity_id, :as_of_date, :cat_sightings)"
                ),
                {
                    "entity_id": complaint[0],
                    "as_of_date": complaint[1],
                    "cat_sightings": complaint[2],
                },
            )

        conn.execute(text("""
                create table events (
                entity_id int,
                outcome int,
                outcome_date date
                )
                """))

        for event in events:
            conn.execute(
                text("insert into events values (:entity_id, :outcome, :outcome_date)"),
                {
                    "entity_id": event[0],
                    "outcome": event[1],
                    "outcome_date": event[2],
                },
            )


def sample_cohort_config(query_source="filepath"):
    return {
        "name": "has_past_events",
        query_source: CONFIG_QUERY_DATA["cohort"][query_source],
    }


def sample_config(query_source="filepath"):
    temporal_config = {
        "feature_start_time": "2010-01-01",
        "feature_end_time": "2014-01-01",
        "label_start_time": "2011-01-01",
        "label_end_time": "2015-01-01",
        "model_update_frequency": "1year",
        "training_label_timespans": ["12months"],
        "test_label_timespans": ["12months"],
        "training_as_of_date_frequencies": "1day",
        "test_as_of_date_frequencies": "3day",
        "max_training_histories": ["10years"],
        "test_durations": ["1months"],
    }

    scoring_config = {
        "testing_metric_groups": [
            {"metrics": ["precision@"], "thresholds": {"top_n": [2]}}
        ],
        "training_metric_groups": [
            {"metrics": ["precision@"], "thresholds": {"top_n": [3]}}
        ],
        "subsets": [
            {
                "name": "evens",
                "query": """\
                    select distinct entity_id
                    from events
                    where entity_id % 2 = 0
                    and outcome_date < '{as_of_date}'::date
                """,
            },
        ],
    }

    grid_config = {
        "sklearn.tree.DecisionTreeClassifier": {
            "min_samples_split": [10, 100],
            "max_depth": [3, 5],
            "criterion": ["gini"],
        }
    }

    feature_config = [
        {
            "prefix": "entity_features",
            "from_obj": "cat_complaints",
            "knowledge_date_column": "as_of_date",
            "aggregates_imputation": {"all": {"type": "constant", "value": 0}},
            "aggregates": [{"quantity": "cat_sightings", "metrics": ["count", "avg"]}],
            "intervals": ["all"],
        },
        {
            "prefix": "zip_code_features",
            "from_obj": "entity_zip_codes join zip_code_events using (zip_code)",
            "knowledge_date_column": "as_of_date",
            "aggregates_imputation": {"all": {"type": "constant", "value": 0}},
            "aggregates": [{"quantity": "num_events", "metrics": ["max", "min"]}],
            "intervals": ["all"],
        },
    ]

    cohort_config = sample_cohort_config(query_source)

    label_config = {
        query_source: CONFIG_QUERY_DATA["label"][query_source],
        "name": "custom_label_name",
        "include_missing_labels_in_train_as": False,
    }

    # bias_audit_config disabled: Aequitas removed (ADR-0007); SQL bias group-bys
    # will replace it in a later phase.

    return {
        "config_version": CONFIG_VERSION,
        "random_seed": 1234,
        "label_config": label_config,
        "entity_column_name": "entity_id",
        "model_comment": "test2-final-final",
        "model_group_keys": [
            "label_name",
            "label_type",
            "custom_key",
            "class_path",
            "parameters",
        ],
        "feature_aggregations": feature_config,
        "cohort_config": cohort_config,
        "temporal_config": temporal_config,
        "grid_config": grid_config,
        # bias_audit_config disabled: Aequitas removed (ADR-0007).
        "prediction": {"rank_tiebreaker": "random"},
        "scoring": scoring_config,
        "user_metadata": {"custom_key": "custom_value"},
    }


@contextmanager
def assert_plot_figures_added():
    num_figures_before = plt.gcf().number
    yield
    num_figures_after = plt.gcf().number
    assert num_figures_before < num_figures_after


class CallSpy:
    """Callable-wrapper and -patcher to record invocations.

    ``CallSpy``, (unlike ``Mock``), makes it easy to wrap callables for
    the express purpose of recording how they're invoked – without
    modifying functionality. And ``CallSpy``, (unlike ``Mock``),
    reproduces the descriptor interface, such that methods can be
    patched and proxied for this purpose, as easily as functions.

    For example, as a context manager::

        with CallSpy('my_module.MyClass.my_method') as spy:
            ...

        assert (('arg0',), {'param0': 0}) in spy.calls

    """

    def __init__(self, signature):
        self.calls = []
        self.signature = signature

    @cached_property
    def target_path(self):
        return self.signature.split(".")

    @cached_property
    def target_name(self):
        return self.target_path[-1]

    @cached_property
    def target_base(self):
        # walk target path until can no longer import it as a module path
        for index in range(len(self.target_path)):
            path_parts = self.target_path[: (index + 1)]
            import_path = ".".join(path_parts)

            try:
                base = importlib.import_module(import_path)
            except ImportError:
                # we've imported all that we can import
                # walk the remainder by attribute access
                remainder = self.target_path[index:-1]
                for part in remainder:
                    base = getattr(base, part)

                return base

        raise ValueError(f"cannot patch signature {self.signature!r}")

    @cached_property
    def target_object(self):
        return getattr(self.target_base, self.target_name)

    @cached_property
    def patch(self):
        return mock.patch.object(self.target_base, self.target_name, new=self)

    def start(self):
        if not callable(self.target_object):
            # 1. ensure target_object set before patching
            # 2. check that it's sane (needn't be done here but reasonable)
            raise TypeError(f"signature target not callable {self.target_object!r}")

        self.patch.start()

    def stop(self):
        self.patch.stop()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.target_object(*args, **kwargs)

    def __get__(self, instance, cls=None):
        if instance is None:
            return self

        return functools.partial(self, instance)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
