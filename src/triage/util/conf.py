from triage.logging import get_logger

logger = get_logger(__name__)

import copy
import os
import re
from datetime import datetime

import yaml
from dateutil.relativedelta import relativedelta


def parse_from_obj(config, alias):
    """
    Parses a from_obj configuration key. If it's a from_obj_table just returns it.
    If it's a from_obj_query creates the sub_query with alias
    Args:
        config: the yaml dict
        alias: the name of the alias if there's a from_obj_query

    Returns:

    """
    from_obj = config.get("from_obj_table", None)
    if not from_obj:
        from_obj = config.get("from_obj_query", None)
        return " ({}) {} ".format(from_obj, alias) if from_obj else None
    return from_obj


def dt_from_str(dt_str):
    if isinstance(dt_str, datetime):
        return dt_str
    return datetime.strptime(dt_str, "%Y-%m-%d")


_DELTA_PATTERN = re.compile(r"^(\d+) *([^ ]+)$")


def parse_delta_string(delta_string):
    """Given a string in a postgres interval format (e.g., '1 month'),
    parse the units and value from it.

    Assumptions:
    - The string is in the format 'value unit', where
      value is an int and unit is one of year(s), month(s), day(s),
      week(s), hour(s), minute(s), second(s), microsecond(s), or an
      abbreviation matching y, d, w, h, m, s, or ms (case-insensitive).
      For example: 1 year, 1year, 2 years, 1 y, 2y, 1Y.

    :param delta_string: the time interval to convert
    :type delta_string: str

    :return: time units, number of units (value)
    :rtype: tuple

    :raises: ValueError if the delta_string is not in the expected format

    """
    match = _DELTA_PATTERN.search(delta_string)
    if match:
        pre_value, units = match.groups()
        return (units, int(pre_value))

    raise ValueError(
        "Could not parse value from time delta string: {!r}".format(delta_string)
    )


def load_query_if_needed(config_component):
    """Load the cohort or label query from a file

    Args:
        config_component (dict) A cohort or label config

    Returns: None
    """
    config_component_copy = copy.copy(config_component)
    if "filepath" in config_component_copy:
        logger.warning(
            "Loading query from file; if there is a query in the config, it will be overwritten"
        )

        query_filename = os.path.join(
            os.path.abspath(os.getcwd()), config_component_copy["filepath"]
        )

        with open(query_filename) as f:
            config_component_copy["query"] = f.read()

        config_component_copy.pop("filepath")

    return config_component_copy


_VERBOSE_UNIT_PATTERN = re.compile(
    r"^(year|month|day|week|hour|minute|second|microsecond)s?$"
)

_BRIEF_UNITS = {
    "y": "years",
    "d": "days",
    "w": "weeks",
    "h": "hours",
    "m": "minutes",
    "s": "seconds",
    "ms": "microseconds",
}


def convert_str_to_relativedelta(delta_string):
    """Given a string in a postgres interval format (e.g., '1 month'),
    convert it to a dateutil.relativedelta.relativedelta.

    Assumptions:
    - The string is in the format 'value unit', where
      value is an int and unit is one of year(s), month(s), day(s),
      week(s), hour(s), minute(s), second(s), microsecond(s), or an
      abbreviation matching y, d, w, h, m, s, or ms (case-insensitive).
      For example: 1 year, 1year, 2 years, 1 y, 2y, 1Y.

    :param delta_string: the time interval to convert
    :type delta_string: str

    :return: the time interval as a relativedelta
    :rtype: dateutil.relativedelta.relativedelta

    :raises: ValueError if the delta_string is not in the expected format

    """
    units, value = parse_delta_string(delta_string)

    # value is an int count of a validated plural unit (years/months/…); the
    # dynamic **kwarg spread is a real relativedelta keyword, not the dt1/dt2
    # positional dates pyright matches it against.
    verbose_match = _VERBOSE_UNIT_PATTERN.search(units)
    if verbose_match:
        kwargs = {verbose_match.group(1) + "s": value}
        return relativedelta(**kwargs)  # pyright: ignore[reportArgumentType]

    try:
        unit_type = _BRIEF_UNITS[units.lower()]
    except KeyError:
        pass
    else:
        if unit_type == "minutes":
            logger.warning(f'Time delta units "{units}" converted to minutes.')
        kwargs = {unit_type: value}
        return relativedelta(**kwargs)  # pyright: ignore[reportArgumentType]

    raise ValueError("Could not handle units. Units: {} Value: {}".format(units, value))
