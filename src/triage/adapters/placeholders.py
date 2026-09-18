"""One rendering for the ``{as_of_date}`` placeholder, whichever config block carries it (#11).

Four config blocks template a query on ``{as_of_date}``, and they did not agree on what the token
meant:

* ``cohort_config.query`` and ``label_config.query`` substitute a **quoted literal**
  (``cohort.py``, ``labels.py``: ``format(as_of_date=f"'{as_of_date}'")``), so every shipped
  cohort and label example writes ``where d < {as_of_date}::date``.
* ``evaluation.subsets[].query`` and ``bias_config.query`` substituted the date **bare**, so that
  same spelling rendered ``where d < 2022-10-01::date`` — which PostgreSQL parses as the integer
  expression ``2022 - 10 - 1`` and rejects with ``cannot cast type integer to date``. Their own
  documented examples therefore had to quote the placeholder, which in turn is a syntax error in
  a cohort query.

Nothing said the blocks disagreed, and ``validate_experiment_config`` only checked the token was
present. The failure surfaced at run time, after the cohort and the labels had already been built.

:func:`render_as_of_date` removes the disagreement without breaking either spelling: a quote pair
already wrapping the placeholder is stripped, then the quoted literal is substituted as the cohort
and label paths do. ``{as_of_date}::date``, ``'{as_of_date}'`` and ``date '{as_of_date}'`` all
render to the same SQL. There is no legitimate bare rendering to preserve — it was always a
syntax error.
"""

from __future__ import annotations

import re

__all__ = ["render_as_of_date"]

#: A ``{as_of_date}`` the author already wrapped in single quotes. Anchored on the placeholder, so
#: an unrelated literal elsewhere in the query (``where status = 'OPEN'``) is never touched.
_QUOTED_PLACEHOLDER = re.compile(r"'\{as_of_date\}'")


def render_as_of_date(query: str, date_str: str) -> str:
    """Render ``{as_of_date}`` as a quoted SQL literal, accepting a pre-quoted placeholder."""
    return _QUOTED_PLACEHOLDER.sub("{as_of_date}", query).format(
        as_of_date=f"'{date_str}'"
    )
