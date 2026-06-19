import datetime
import re

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from triage.component.catwalk.utils import (
    filename_friendly_hash,
    sort_predictions_and_labels,
)


def test_filename_friendly_hash():
    data = {
        "stuff": "stuff",
        "other_stuff": "more_stuff",
        "a_datetime": datetime.datetime(2015, 1, 1),
        "a_date": datetime.date(2016, 1, 1),
        "a_number": 5.0,
    }
    output = filename_friendly_hash(data)
    assert isinstance(output, str)
    assert re.match(r"^[\w]+$", output) is not None

    # make sure ordering keys differently doesn't change the hash
    new_output = filename_friendly_hash(
        {
            "other_stuff": "more_stuff",
            "stuff": "stuff",
            "a_datetime": datetime.datetime(2015, 1, 1),
            "a_date": datetime.date(2016, 1, 1),
            "a_number": 5.0,
        }
    )
    assert new_output == output

    # make sure new data hashes to something different
    new_output = filename_friendly_hash({"stuff": "stuff", "a_number": 5.0})
    assert new_output != output


def test_filename_friendly_hash_stability():
    nested_data = {"one": "two", "three": {"four": "five", "six": "seven"}}
    output = filename_friendly_hash(nested_data)
    # 1. we want to make sure this is stable across different runs
    # so hardcode an expected value
    assert output == "9a844a7ebbfd821010b1c2c13f7391e6"
    other_nested_data = {"one": "two", "three": {"six": "seven", "four": "five"}}
    new_output = filename_friendly_hash(other_nested_data)
    assert output == new_output


def test_sort_predictions_and_labels():
    predictions = np.array([0.5, 0.4, 0.6, 0.5, 0.6])
    entities = np.array(range(5))
    labels = np.array([0, 0, 1, 1, None])

    # best sort
    sorted_predictions, sorted_labels, sorted_entities = sort_predictions_and_labels(
        predictions, labels, entities, tiebreaker="best"
    )
    assert_array_equal(sorted_predictions, np.array([0.6, 0.6, 0.5, 0.5, 0.4]))
    assert_array_equal(sorted_labels, np.array([1, None, 1, 0, 0]))
    assert_array_equal(sorted_entities.to_numpy(), np.array([2, 4, 3, 0, 1]))

    # worst sort
    sorted_predictions, sorted_labels, sorted_entities = sort_predictions_and_labels(
        predictions, labels, entities, tiebreaker="worst"
    )
    assert_array_equal(sorted_predictions, np.array([0.6, 0.6, 0.5, 0.5, 0.4]))
    assert_array_equal(sorted_labels, np.array([None, 1, 0, 1, 0]))
    assert_array_equal(sorted_entities.to_numpy(), np.array([4, 2, 0, 3, 1]))

    # random tiebreaker needs a seed
    with pytest.raises(ValueError):
        sort_predictions_and_labels(predictions, labels, entities, tiebreaker="random")

    # random tiebreaker respects the seed
    sorted_predictions, sorted_labels, sorted_entities = sort_predictions_and_labels(
        predictions, labels, entities, tiebreaker="random", sort_seed=1234
    )
    assert_array_equal(sorted_predictions, np.array([0.6, 0.6, 0.5, 0.5, 0.4]))
    assert_array_equal(sorted_labels, np.array([None, 1, 1, 0, 0]))
    assert_array_equal(sorted_entities.to_numpy(), np.array([4, 2, 3, 0, 1]))

    sorted_predictions, sorted_labels, sorted_entities = sort_predictions_and_labels(
        predictions, labels, entities, tiebreaker="random", sort_seed=24376234
    )
    assert_array_equal(sorted_predictions, np.array([0.6, 0.6, 0.5, 0.5, 0.4]))
    assert_array_equal(sorted_labels, np.array([None, 1, 0, 1, 0]))
    assert_array_equal(sorted_entities.to_numpy(), np.array([4, 2, 0, 3, 1]))
