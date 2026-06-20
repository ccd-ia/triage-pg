import datetime
import hashlib
import json
import random

import numpy as np
import pandas as pd
import psycopg
from retrying import retry

from triage.logging import get_logger

logger = get_logger(__name__)


def filename_friendly_hash(inputs):
    def dt_handler(x):
        if isinstance(x, datetime.datetime) or isinstance(x, datetime.date):
            return x.isoformat()
        raise TypeError("Unknown type")

    return hashlib.md5(
        json.dumps(inputs, default=dt_handler, sort_keys=True).encode("utf-8")
    ).hexdigest()


def get_subset_table_name(subset_config):
    return "subset_{}_{}".format(
        subset_config.get("name", "default"),
        filename_friendly_hash(subset_config),
    )


def retry_if_db_error(exception):
    return isinstance(exception, psycopg.OperationalError)


DEFAULT_RETRY_KWARGS = {
    "retry_on_exception": retry_if_db_error,
    "wait_exponential_multiplier": 1000,  # wait 2^x*1000ms between each retry
    "stop_max_attempt_number": 14,
    # with this configuration, last wait will be ~2 hours
    # for a total of ~4.5 hours waiting
}


db_retry = retry(**DEFAULT_RETRY_KWARGS)


class Batch:
    # modified from
    # http://codereview.stackexchange.com/questions/118883/split-up-an-iterable-into-batches
    def __init__(self, iterable, limit=None):
        self.iterator = iter(iterable)
        self.limit = limit
        try:
            self.current = next(self.iterator)
        except StopIteration:
            self.on_going = False
        else:
            self.on_going = True

    def group(self):
        yield self.current
        # start enumerate at 1 because we already yielded the last saved item
        for num, item in enumerate(self.iterator, 1):
            self.current = item
            if num == self.limit:
                break
            yield item
        else:
            self.on_going = False

    def __iter__(self):
        while self.on_going:
            yield self.group()


AVAILABLE_TIEBREAKERS = {"random", "best", "worst"}


def sort_predictions_and_labels(
    predictions_proba, labels, df_index, tiebreaker="random", sort_seed=None
):
    """Sort predictions and labels with a configured tiebreaking rule

    Args:
        predictions_proba (np.array) The predicted scores
        labels (np.array) The numeric labels (1/0, not True/False)
        df_index (pd.MultiIndex) Index (generally entity_id, as_of_date tuples) to be sorted with the labels/scores
        tiebreaker (string) The tiebreaking method ('best', 'worst', 'random')
        sort_seed (signed int) The sort seed. Needed if 'random' tiebreaking is picked.

    Returns:
        (tuple) (predictions_proba, labels, df_index), sorted
    """
    if len(labels) == 0:
        logger.notice("No labels present, skipping predictions sorting .")
        return (predictions_proba, labels, df_index)

    df = pd.DataFrame(predictions_proba, columns=["score"])
    df["label_value"] = labels
    df.set_index(df_index, inplace=True)

    if tiebreaker == "random":
        if not sort_seed:
            raise ValueError("If random tiebreaker is used, a sort seed must be given")
        random.seed(sort_seed)
        np.random.seed(sort_seed)
        df["random"] = np.random.rand(len(df))
        df.sort_values(by=["score", "random"], inplace=True, ascending=[False, False])
        df.drop("random", axis=1)
    elif tiebreaker == "worst":
        df.sort_values(
            by=["score", "label_value"],
            inplace=True,
            ascending=[False, True],
            na_position="first",
        )
    elif tiebreaker == "best":
        df.sort_values(
            by=["score", "label_value"],
            inplace=True,
            ascending=[False, False],
            na_position="last",
        )
    else:
        raise ValueError(f"Unknown tiebreaker: {tiebreaker}")

    return [df["score"].to_numpy(), df["label_value"].to_numpy(), df.index]
