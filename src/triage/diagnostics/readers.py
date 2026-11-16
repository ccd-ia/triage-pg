"""The shared query layer: what the dashboard routes and the plot commands read.

Each function is a ``select`` over a ``triage.*`` table, view or SQL function, run through a
:data:`~triage.util.db.DictRowPool`, so every number it returns is the database's. The
module imports no FastAPI and no plotting library: it works with the core install. The
dashboard routes wrap these functions in HTTP (their 404s, 422s and empty-state envelopes
stay in the routes), and anything that draws reads the same rows, so a figure and a panel
cannot disagree.

Errors carry no HTTP or CLI vocabulary:

* :class:`NotFoundError` (a ``ValueError``) — an id, name or hash prefix the caller passed
  matches nothing. It is bad input: a route answers 404.
* ``ValueError`` — an option outside what the reader accepts.

Missing data is not an error. A reader returns an empty list, or ``None`` rates, and the
caller decides how to say so.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, LiteralString, Optional

from triage.util.db import DictRowPool

#: The kinds of error-tree leaf ``triage.error_analysis`` holds.
ERROR_KINDS = ("fp", "fn", "confusion", "abs_error", "beats_baseline")

#: The leaf kinds of a regression model (migration 0034): they take no top-k cut.
REGRESSION_ERROR_KINDS = ("abs_error", "beats_baseline")

#: The crosstab statistics a reader ranks: the ratio of means, or the nonzero rates.
CROSSTAB_STATS = ("mean", "nonzero_rate")

#: What a crosstab compares the selected rows with (migration 0037).
CROSSTAB_COMPARISONS = ("rest", "near_miss")

DateLike = date | str


class NotFoundError(ValueError):
    """An id, name or hash prefix that matches nothing in the project database."""


def _rows(
    pool: DictRowPool, sql: LiteralString, params: Optional[dict[str, Any]] = None
) -> list[dict[str, Any]]:
    # LiteralString: every query here is a literal, which keeps dynamically built SQL (an
    # injection surface) out of the read layer by construction.
    with pool.connection() as conn:
        return conn.execute(sql, params or {}).fetchall()


def _one(
    pool: DictRowPool, sql: LiteralString, params: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
    with pool.connection() as conn:
        return conn.execute(sql, params or {}).fetchone()


def _check_error_kind(error_kind: Optional[str]) -> None:
    if error_kind is not None and error_kind not in ERROR_KINDS:
        raise ValueError(
            f"{error_kind!r} is not an error-tree leaf kind — expected one of"
            f" {', '.join(ERROR_KINDS)}"
        )


def _check_points(points: Optional[int]) -> None:
    if points is not None and points < 2:
        raise ValueError(
            f"points={points} cannot span the population — pass 2 or more (101 gives"
            " every whole percent), or None for every k"
        )


def _require_subset(pool: DictRowPool, subset_hash: str) -> None:
    if (
        _one(
            pool,
            "select 1 as found from triage.subsets where subset_hash = %(s)s",
            {"s": subset_hash},
        )
        is None
    ):
        raise NotFoundError(
            f"subset {subset_hash!r} not found — list the subset hashes with:"
            " select subset_hash, config->>'name' as name from triage.subsets;"
        )


def _require_group(pool: DictRowPool, model_group_id: int) -> None:
    if (
        _one(
            pool,
            "select 1 as found from triage.model_groups where model_group_id = %(g)s",
            {"g": model_group_id},
        )
        is None
    ):
        raise NotFoundError(
            f"model group {model_group_id} not found — list a project's groups with:"
            " triage models <experiment>"
        )


# ------------------------------------------------------------------------------- subsets


def resolve_subset(pool: DictRowPool, value: str) -> tuple[str, Optional[str]]:
    """A Subset by its name or by (a prefix of) its subset_hash.

    Args:
        pool: the project database.
        value: a Subset name, or a subset_hash or a prefix of one.

    Returns:
        ``(subset_hash, name)``.

    Raises:
        NotFoundError: no Subset matches ``value``.
        ValueError: more than one Subset matches; the message names them.
    """
    rows = _rows(
        pool,
        "select subset_hash, config->>'name' as name from triage.subsets"
        " where config->>'name' = %(v)s"
        "    or left(subset_hash, length(%(v)s)) = %(v)s"
        " order by subset_hash limit 5",
        {"v": value},
    )
    if not rows:
        raise NotFoundError(
            f"No subset matches {value!r} by name or subset_hash — list them with:"
            " select subset_hash, config->>'name' as name from triage.subsets;"
        )
    if len(rows) > 1:
        raise ValueError(
            f"Ambiguous subset {value!r} — matches "
            + ", ".join(f"{r['name']} ({r['subset_hash'][:12]}…)" for r in rows)
            + ". Pass the full subset_hash."
        )
    return rows[0]["subset_hash"], rows[0]["name"]


# -------------------------------------------------------------------------------- curves


def _curve(
    pool: DictRowPool,
    model_id: int,
    as_of_date: Optional[DateLike],
    subset: str,
    points: Optional[int],
) -> list[dict[str, Any]]:
    params = {
        "model_id": model_id,
        "as_of_date": None if as_of_date is None else str(as_of_date),
        "subset": subset,
        "points": points,
    }
    if points is None:
        return _rows(
            pool,
            "select k, pct, prec, rec, tp, fp, fn, tn, base_rate"
            " from triage.model_threshold_curve(%(model_id)s, %(as_of_date)s::date,"
            "                                   %(subset)s)"
            " order by k",
            params,
        )
    return _rows(
        pool,
        "with c as ("
        "    select k, pct, prec, rec, tp, fp, fn, tn, base_rate"
        "    from triage.model_threshold_curve(%(model_id)s, %(as_of_date)s::date,"
        "                                      %(subset)s)"
        "), n as ("
        "    select max(k) as n from c"
        "), d as ("
        # the date the curve function draws: the one given, else the latest test date
        "    select coalesce(%(as_of_date)s::date,"
        "                    (select max(p.as_of_date) from triage.predictions p"
        "                      where p.model_id = %(model_id)s"
        "                        and p.split_kind = 'test')) as d"
        "), cuts as ("
        "    select distinct e.parameter"
        "    from triage.evaluations e cross join d"
        "    where e.model_id = %(model_id)s and e.split_kind = 'test'"
        "      and e.as_of_date = d.d and e.metric = 'precision@'"
        "), keep as ("
        # the row nearest each of `points` evenly spaced shares of the population, 0 to 1
        "    select greatest(1, round(i * n.n::numeric / (%(points)s::int - 1)))::int as k"
        "    from n cross join generate_series(0, %(points)s::int - 1) as i"
        "    union"
        # each configured cut's exact row, so the thinned curve meets the stored metrics
        "    select triage.resolve_k(cuts.parameter, n.n) from n cross join cuts"
        "    union"
        "    select n.n from n"
        ")"
        " select c.k, c.pct, c.prec, c.rec, c.tp, c.fp, c.fn, c.tn, c.base_rate"
        " from c"
        " where c.k in (select keep.k from keep)"
        " order by c.k",
        params,
    )


def curve(
    pool: DictRowPool,
    model_id: int,
    as_of_date: Optional[DateLike] = None,
    subset: str = "",
    points: Optional[int] = None,
) -> list[dict[str, Any]]:
    """The Rayid curve of one test date: precision, recall and the confusion counts at each
    population cut, with the population's base rate.

    Reads ``triage.model_threshold_curve``, so the row at k agrees with the stored
    ``precision@``/``recall@`` at k.

    Args:
        pool: the project database.
        model_id: the model.
        as_of_date: the test date; ``None`` reads the model's latest test date.
        subset: a subset_hash; ``''`` is everyone. A Subset is its own population: its
            members are ranked among themselves.
        points: ``None`` returns every k. ``N`` (2 or more) keeps the rows nearest to N
            evenly spaced shares of the population, from 0 to 1, plus the exact row of each
            configured cut (``resolve_k`` of every ``precision@`` parameter stored for the
            model at that date) and the last row. A thinned curve therefore still equals the
            stored metrics at the cuts.

    Returns:
        Rows ``k, pct, prec, rec, tp, fp, fn, tn, base_rate``, ordered by k. Empty when the
        model has no test predictions at the date, or the Subset no members there.

    Raises:
        NotFoundError: ``subset`` is not a known subset_hash.
        ValueError: ``points`` is below 2.
    """
    _check_points(points)
    if subset:
        _require_subset(pool, subset)
    return _curve(pool, model_id, as_of_date, subset, points)


def group_curves(
    pool: DictRowPool,
    model_group_id: int,
    subset: str = "",
    points: Optional[int] = 101,
    split: str = "test",
    experiment_hash: Optional[str] = None,
) -> list[dict[str, Any]]:
    """One curve per (model, test date) of a model group: the curve across test dates.

    Args:
        pool: the project database.
        model_group_id: the model group.
        subset: a subset_hash; ``''`` is everyone.
        points: thinning, as in :func:`curve`; ``None`` keeps every k.
        split: the split the curves are drawn over. The threshold curve is defined on test
            predictions only, so ``'test'`` is the one value accepted.
        experiment_hash: keep only the models of this experiment's runs. A model group is
            shared by every experiment that trains the same configuration.

    Returns:
        Series ordered by test date, then model: ``model_id``, ``as_of_date``,
        ``base_rate`` (the population's), ``population`` (its scored rows) and ``points``
        (the :func:`curve` rows). A (model, date) whose curve is empty — a Subset without
        members there — has no series.

    Raises:
        NotFoundError: the model group, or ``subset``, is unknown.
        ValueError: ``split`` is not ``'test'``, or ``points`` is below 2.
    """
    if split != "test":
        raise ValueError(
            f"split={split!r}: the threshold curve is drawn over test predictions only"
            " — pass split='test'"
        )
    _check_points(points)
    _require_group(pool, model_group_id)
    if subset:
        _require_subset(pool, subset)
    pairs = _rows(
        pool,
        "select distinct p.model_id, p.as_of_date"
        " from triage.predictions p"
        " join triage.models m on m.model_id = p.model_id"
        " where m.model_group_id = %(g)s and p.split_kind = 'test'"
        "   and (%(exp)s::text is null or exists ("
        "        select 1 from triage.runs r"
        "        where r.run_id = m.run_id and r.experiment_hash = %(exp)s))"
        " order by p.as_of_date, p.model_id",
        {"g": model_group_id, "exp": experiment_hash},
    )
    series: list[dict[str, Any]] = []
    for pair in pairs:
        rows = _curve(pool, pair["model_id"], pair["as_of_date"], subset, points)
        if not rows:
            continue
        series.append(
            {
                "model_id": pair["model_id"],
                "as_of_date": pair["as_of_date"],
                "base_rate": rows[-1]["base_rate"],
                "population": rows[-1]["k"],
                "points": rows,
            }
        )
    return series


# ----------------------------------------------------------------------------- crosstabs


def crosstabs(
    pool: DictRowPool,
    model_id: int,
    parameter: Optional[str] = None,
    stat: str = "mean",
    limit: int = 20,
    as_of_date: Optional[DateLike] = None,
    comparison: str = "rest",
) -> list[dict[str, Any]]:
    """The features that most distinguish the selected top-k from the rest.

    Reads ``triage.crosstabs`` (written by ``triage postmodel crosstabs``).

    Args:
        pool: the project database.
        model_id: the model.
        parameter: the top-k cut; ``None`` reads every cut persisted.
        stat: ``'mean'`` ranks by |ln(selected mean / rest mean)|, over rows with a
            positive ratio. ``'nonzero_rate'`` ranks by |selected rate − rest rate|,
            because a ratio of two rates near zero misleads.
        limit: the most rows returned.
        as_of_date: one prediction date; ``None`` reads every date.
        comparison: ``'rest'`` (every row below the cut) or ``'near_miss'`` (the next k
            rows, ``triage postmodel crosstabs --against near-miss``); ``rest_value``
            holds the comparison's value.

    Returns:
        Rows ``as_of_date, parameter, feature, selected_value, rest_value, ratio``, the most
        distinguishing first. Empty when nothing is persisted for the model.

    Raises:
        ValueError: ``stat`` is not one of :data:`CROSSTAB_STATS`, or ``comparison`` is
            not one of :data:`CROSSTAB_COMPARISONS`.
    """
    if comparison not in CROSSTAB_COMPARISONS:
        raise ValueError(
            f"comparison={comparison!r} is not a crosstab comparison — expected one of"
            f" {', '.join(CROSSTAB_COMPARISONS)}"
        )
    params: dict[str, Any] = {
        "m": model_id,
        "p": parameter,
        "d": None if as_of_date is None else str(as_of_date),
        "limit": limit,
        "c": comparison,
    }
    if stat == "mean":
        return _rows(
            pool,
            "select as_of_date, parameter, feature, selected_value, rest_value, ratio"
            " from triage.crosstabs"
            " where model_id = %(m)s and stat = 'mean' and ratio is not null and ratio > 0"
            "   and comparison = %(c)s"
            "   and (%(p)s::text is null or parameter = %(p)s)"
            "   and (%(d)s::date is null or as_of_date = %(d)s::date)"
            " order by abs(ln(ratio)) desc, as_of_date, parameter, feature"
            " limit %(limit)s",
            params,
        )
    if stat == "nonzero_rate":
        return _rows(
            pool,
            "select as_of_date, parameter, feature, selected_value, rest_value, ratio"
            " from triage.crosstabs"
            " where model_id = %(m)s and stat = 'nonzero_rate'"
            "   and selected_value is not null and rest_value is not null"
            "   and comparison = %(c)s"
            "   and (%(p)s::text is null or parameter = %(p)s)"
            "   and (%(d)s::date is null or as_of_date = %(d)s::date)"
            " order by abs(selected_value - rest_value) desc, as_of_date, parameter,"
            "          feature"
            " limit %(limit)s",
            params,
        )
    raise ValueError(
        f"stat={stat!r} is not a crosstab ranking — expected one of"
        f" {', '.join(CROSSTAB_STATS)}"
    )


# --------------------------------------------------------------------------- error trees


def error_reference_rates(
    pool: DictRowPool, model_id: int, parameter: str, as_of_date: Optional[DateLike]
) -> dict[str, Optional[float]]:
    """The population error rates at a cut: what each error-tree leaf is read against.

    Read from ``triage.model_threshold_curve`` at k = ``resolve_k(parameter, n)``, where n
    is the scored population of the test date — the cut ``precision@{parameter}`` uses.

    Args:
        pool: the project database.
        model_id: the model.
        parameter: the top-k cut, e.g. ``'10_pct'``.
        as_of_date: the test date; ``None`` reads the model's latest test date.

    Returns:
        ``fp`` = fp / (tp + fp), the wrong share of the selected (1 − precision@k);
        ``fn`` = fn / (fn + tn), the missed share of the labeled rest; and ``confusion`` =
        (fp + fn) / labeled. Each is ``None`` when its denominator is zero, or when the
        model has no test predictions at the date.
    """
    row = _one(
        pool,
        "with c as ("
        "    select k, tp, fp, fn, tn"
        "    from triage.model_threshold_curve(%(m)s, %(d)s::date)"
        ")"
        " select c.tp, c.fp, c.fn, c.tn"
        " from c"
        " where c.k = triage.resolve_k(%(p)s, (select max(k) from c))",
        {
            "m": model_id,
            "d": None if as_of_date is None else str(as_of_date),
            "p": parameter,
        },
    )
    if row is None:
        return {"fp": None, "fn": None, "confusion": None}
    tp, fp, fn, tn = row["tp"], row["fp"], row["fn"], row["tn"]
    labeled = tp + fp + fn + tn
    return {
        "fp": fp / (tp + fp) if tp + fp else None,
        "fn": fn / (fn + tn) if fn + tn else None,
        "confusion": (fp + fn) / labeled if labeled else None,
    }


def _with_reference_rates(
    pool: DictRowPool, model_id: int, rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each leaf gains ``reference_error_rate``: its kind's population rate at its cut.

    An ``abs_error`` leaf's reference is 1: its error rate is its mean |e| over the date's
    population MAE. A ``beats_baseline`` leaf's is the share of the training rows where the
    model is not closer (``triage.error_tree_fits``). A survival tree at a horizon
    (parameter ``<cut>|horizon=<H>``) reads the errors of all its leaves over their rows.
    """
    from triage.diagnostics.error_tree import HORIZON_MARK

    rates: dict[tuple[Any, str], dict[str, Optional[float]]] = {}
    fits: dict[str, Optional[float]] = {}
    pooled: dict[tuple[Any, str, str], Optional[float]] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["error_kind"] == "abs_error":
            row["reference_error_rate"] = 1.0
        elif row["error_kind"] == "beats_baseline":
            if row["parameter"] not in fits:
                fit = _one(
                    pool,
                    "select 1 - base_rate_train as rate from triage.error_tree_fits"
                    " where model_id = %(m)s and error_kind = 'beats_baseline'"
                    "   and parameter = %(p)s",
                    {"m": model_id, "p": row["parameter"]},
                )
                fits[row["parameter"]] = None if fit is None else fit["rate"]
            row["reference_error_rate"] = fits[row["parameter"]]
        elif HORIZON_MARK in row["parameter"]:
            # a survival tree at a horizon: its outcome is not in the threshold curve, and
            # its leaves partition the tree's rows, so the pooled leaf rate is the
            # population's
            key = (row["as_of_date"], row["parameter"], row["error_kind"])
            if key not in pooled:
                found = _one(
                    pool,
                    "select sum(n_errors)::double precision / nullif(sum(n_matched), 0)"
                    "       as rate"
                    " from triage.error_analysis"
                    " where model_id = %(m)s"
                    "   and split_kind = cast(%(s)s as triage.split_kind)"
                    "   and as_of_date = %(d)s and parameter = %(p)s"
                    "   and error_kind = %(k)s",
                    {
                        "m": model_id,
                        "s": row["split_kind"],
                        "d": row["as_of_date"],
                        "p": row["parameter"],
                        "k": row["error_kind"],
                    },
                )
                pooled[key] = None if found is None else found["rate"]
            row["reference_error_rate"] = pooled[key]
        elif row["split_kind"] != "test":
            row["reference_error_rate"] = None  # the curve is drawn over test rows only
        else:
            key = (row["as_of_date"], row["parameter"])
            if key not in rates:
                rates[key] = error_reference_rates(
                    pool, model_id, row["parameter"], row["as_of_date"]
                )
            row["reference_error_rate"] = rates[key][row["error_kind"]]
        out.append(row)
    return out


def error_rules(
    pool: DictRowPool,
    model_id: int,
    error_kind: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """The error-tree leaves of a model, the highest error rate first.

    Reads ``triage.error_analysis`` (written by ``triage postmodel error-tree``).

    Args:
        pool: the project database.
        model_id: the model.
        error_kind: keep one kind of leaf (``fp``, ``fn`` or ``confusion``); ``None`` keeps
            all.
        limit: the most rows returned.

    Returns:
        Rows ``as_of_date, parameter, error_kind, rule, n_matched, n_errors, error_rate,
        depth, class_counts, signature, leaf_stats, reference_error_rate``. The reference
        is the population rate of the leaf's kind at its date and cut
        (:func:`error_reference_rates`). ``leaf_stats`` holds a regression leaf's own
        numbers and is ``None`` on the classification kinds. Empty when nothing is
        persisted.

    Raises:
        ValueError: ``error_kind`` is not one of :data:`ERROR_KINDS`.
    """
    _check_error_kind(error_kind)
    rows = _rows(
        pool,
        "select split_kind, as_of_date, parameter, error_kind, rule, n_matched,"
        "       n_errors, error_rate, depth, class_counts, signature, leaf_stats"
        " from triage.error_analysis"
        " where model_id = %(m)s and (%(k)s::text is null or error_kind = %(k)s)"
        " order by error_rate desc, n_matched desc limit %(limit)s",
        {"m": model_id, "k": error_kind, "limit": limit},
    )
    out = _with_reference_rates(pool, model_id, rows)
    for row in out:
        del row[
            "split_kind"
        ]  # read for the reference only; the route never returned it
    return out


def suspicious_leaves(
    pool: DictRowPool,
    model_id: int,
    error_kind: Optional[str] = None,
    max_error_rate: float = 0.02,
    min_support: int = 30,
    limit: int = 20,
    mae_ratio: float = 0.1,
) -> list[dict[str, Any]]:
    """The leaves that are suspiciously right: high support and almost no error.

    Reads ``triage.error_rules_suspicious``: fp leaves with an error rate at most
    ``max_error_rate``, confusion leaves whose TP share is at least 1 − ``max_error_rate``,
    and ``abs_error`` leaves whose mean |e| is at most ``mae_ratio`` times the date's
    population MAE, each holding at least ``min_support`` rows.

    Args:
        pool: the project database.
        model_id: the model.
        error_kind: keep one kind of leaf; ``None`` keeps all.
        max_error_rate: the largest error rate (or non-TP share) a leaf may have.
        min_support: the fewest rows a leaf may hold.
        limit: the most rows returned.
        mae_ratio: the largest mean |e| of an ``abs_error`` leaf, over population MAE.

    Returns:
        Rows ``split_kind, as_of_date, parameter, error_kind, rule_id, rule, n_matched,
        n_errors, error_rate, tp_share, class_counts, signature, precision_at_k, lift,
        mean_abs_error, population_mae, reference_error_rate``, highest lift first. An
        ``abs_error`` leaf's lift is the population MAE over its own. Empty when no leaf qualifies — an
        answer, not a missing diagnostic.

    Raises:
        ValueError: ``error_kind`` is not one of :data:`ERROR_KINDS`.
    """
    _check_error_kind(error_kind)
    rows = _rows(
        pool,
        "select split_kind, as_of_date, parameter, error_kind, rule_id, rule,"
        "       n_matched, n_errors, error_rate, tp_share, class_counts, signature,"
        "       precision_at_k, lift, mean_abs_error, population_mae"
        " from triage.error_rules_suspicious(%(m)s, %(e)s, %(s)s, %(r)s)"
        " where %(k)s::text is null or error_kind = %(k)s"
        " limit %(limit)s",
        {
            "m": model_id,
            "k": error_kind,
            "e": max_error_rate,
            "s": min_support,
            "r": mae_ratio,
            "limit": limit,
        },
    )
    return _with_reference_rates(pool, model_id, rows)


def error_recurrence(
    pool: DictRowPool,
    model_group_id: int,
    parameter: Optional[str] = None,
    error_kind: str = "fp",
    limit: int = 20,
) -> dict[str, Any]:
    """On how many of a model group's test dates each error rule recurs.

    Reads ``triage.error_rule_recurrence``; rules are matched by signature (features and
    split directions, without thresholds).

    Args:
        pool: the project database.
        model_group_id: the model group.
        parameter: the cut; ``None`` reads the most recently analysed one.
        error_kind: ``fp``, ``fn`` or ``confusion``.
        limit: the most rules returned.

    Returns:
        ``model_group_id``, ``parameter`` (the cut read), ``error_kind``, ``parameters``
        (every cut the group's trees of this kind were fitted at, the latest first) and
        ``rules``.
        ``parameters`` and ``rules`` are empty when nothing is persisted for the group.

    Raises:
        NotFoundError: the model group is unknown.
        ValueError: ``error_kind`` is not one of :data:`ERROR_KINDS`.
    """
    _check_error_kind(error_kind)
    _require_group(pool, model_group_id)
    cuts = _rows(
        pool,
        "select ea.parameter"
        " from triage.error_analysis ea"
        " join triage.models m using (model_id)"
        " where m.model_group_id = %(g)s and ea.split_kind = 'test'"
        "   and ea.error_kind = %(k)s"
        " group by ea.parameter"
        " order by max(ea.computed_at) desc, ea.parameter",
        {"g": model_group_id, "k": error_kind},
    )
    parameters = [c["parameter"] for c in cuts]
    if not parameters:
        return {
            "model_group_id": model_group_id,
            "parameter": parameter,
            "error_kind": error_kind,
            "parameters": [],
            "rules": [],
        }
    chosen = parameter if parameter is not None else parameters[0]
    return {
        "model_group_id": model_group_id,
        "parameter": chosen,
        "error_kind": error_kind,
        "parameters": parameters,
        "rules": _rows(
            pool,
            "select signature, n_dates, n_dates_total, mean_error_rate, mean_support,"
            "       example_rule"
            " from triage.error_rule_recurrence(%(g)s, %(p)s, %(k)s)"
            " limit %(limit)s",
            {"g": model_group_id, "p": chosen, "k": error_kind, "limit": limit},
        ),
    }


def group_leaves(
    pool: DictRowPool,
    model_group_id: int,
    parameter: str,
    kinds: Iterable[str] = ("fp", "confusion"),
) -> list[dict[str, Any]]:
    """Every persisted test-date leaf of a model group's error trees at one cut.

    Args:
        pool: the project database.
        model_group_id: the model group.
        parameter: the cut the trees were fitted at.
        kinds: the kinds of leaf to keep.

    Returns:
        Rows ``model_id, as_of_date, parameter, error_kind, rule_id, rule, n_matched,
        n_errors, error_rate, tp_share, wrong_share, depth, class_counts, signature,
        leaf_stats``,
        ordered by date, model, kind and leaf. ``tp_share`` is the leaf's TP count over its
        rows (``None`` on fn leaves). ``wrong_share`` is the share of the leaf the model
        gets wrong: the error rate, or 1 − ``tp_share`` on confusion leaves — the measure
        ``triage.error_rules_suspicious`` thresholds. Empty when nothing is persisted.

    Raises:
        NotFoundError: the model group is unknown.
        ValueError: a kind is not one of :data:`ERROR_KINDS`.
    """
    kinds = list(kinds)
    for kind in kinds:
        _check_error_kind(kind)
    _require_group(pool, model_group_id)
    return _rows(
        pool,
        "select ea.model_id, ea.as_of_date, ea.parameter, ea.error_kind, ea.rule_id,"
        "       ea.rule, ea.n_matched, ea.n_errors, ea.error_rate,"
        "       (ea.class_counts ->> 'tp')::double precision"
        "           / nullif(ea.n_matched, 0) as tp_share,"
        "       case when ea.error_kind = 'confusion'"
        "            then 1 - (ea.class_counts ->> 'tp')::double precision"
        "                     / nullif(ea.n_matched, 0)"
        "            else ea.error_rate end as wrong_share,"
        "       ea.depth, ea.class_counts, ea.signature, ea.leaf_stats"
        " from triage.error_analysis ea"
        " join triage.models m on m.model_id = ea.model_id"
        " where m.model_group_id = %(g)s and ea.split_kind = 'test'"
        "   and ea.parameter = %(p)s and ea.error_kind = any(%(kinds)s)"
        " order by ea.as_of_date, ea.model_id, ea.error_kind, ea.rule_id",
        {"g": model_group_id, "p": parameter, "kinds": kinds},
    )


# ------------------------------------------------------------------------ models, overlap


def group_models(
    pool: DictRowPool, model_group_id: int, experiment_hash: Optional[str] = None
) -> list[dict[str, Any]]:
    """The models of a model group, in training order, each with its first test date.

    Args:
        pool: the project database.
        model_group_id: the model group.
        experiment_hash: keep only the models of this experiment's runs; ``None`` keeps
            every experiment's.

    Returns:
        Rows ``model_id, train_end_time, run_id, training_label_timespan,
        train_duration_ms, test_as_of``. Empty for an unknown group.
    """
    return _rows(
        pool,
        "select m.model_id, m.train_end_time, m.run_id,"
        "       m.training_label_timespan, m.train_duration_ms,"
        "       (select min(e.as_of_date) from triage.evaluations e"
        "          where e.model_id = m.model_id and e.split_kind = 'test'"
        "            and e.subset_hash = '') as test_as_of"
        " from triage.models m"
        " join triage.runs r on r.run_id = m.run_id"
        " where m.model_group_id = %(g)s"
        "   and (%(exp)s::text is null or r.experiment_hash = %(exp)s)"
        " order by m.train_end_time, m.model_id",
        {"g": model_group_id, "exp": experiment_hash},
    )


def list_overlap(
    pool: DictRowPool,
    model_id: int,
    other_model_id: int,
    parameter: str = "100_abs",
    split_kind: str = "test",
) -> list[dict[str, Any]]:
    """Do two models flag the same entities? Their top-k lists on each shared date.

    Reads ``triage.list_overlap``: each model's cut is ``resolve_k`` over its own
    population.

    Args:
        pool: the project database.
        model_id: the first model.
        other_model_id: the second model.
        parameter: the top-k cut.
        split_kind: the prediction split.

    Returns:
        Rows ``as_of_date, k_a, k_b, n_intersection, jaccard, rank_corr``, one per date
        both models scored, ordered by date.
    """
    return _rows(
        pool,
        "select as_of_date, k_a, k_b, n_intersection, jaccard, rank_corr"
        " from triage.list_overlap(%(a)s, %(b)s, %(p)s,"
        "        cast(%(split)s as triage.split_kind), null)"
        " order by as_of_date",
        {"a": model_id, "b": other_model_id, "p": parameter, "split": split_kind},
    )


def list_overlap_sweep(
    pool: DictRowPool,
    model_id: int,
    other_model_id: int,
    parameters: Sequence[str],
    split_kind: str = "test",
) -> list[dict[str, Any]]:
    """How two models' top-k lists agree as k grows: :func:`list_overlap` at each cut.

    Args:
        pool: the project database.
        model_id: the first model.
        other_model_id: the second model.
        parameters: the top-k cuts, in the order given.
        split_kind: the prediction split.

    Returns:
        Rows ``parameter, as_of_date, k_a, k_b, n_intersection, jaccard, rank_corr``: one
        per cut and date both models scored, by cut in the order given, then by date.

    Raises:
        ValueError: ``parameters`` is empty or names a cut twice.
    """
    if not parameters:
        raise ValueError("a sweep needs at least one cut, e.g. 50_abs,100_abs,200_abs")
    repeated = sorted({p for p in parameters if list(parameters).count(p) > 1})
    if repeated:
        raise ValueError(f"the sweep names {', '.join(repeated)} more than once")
    return [
        {"parameter": parameter, **row}
        for parameter in parameters
        for row in list_overlap(pool, model_id, other_model_id, parameter, split_kind)
    ]


# ------------------------------------------------------------------ near miss, prototypes


def _require_model(pool: DictRowPool, model_id: int) -> None:
    if (
        _one(
            pool,
            "select 1 as found from triage.models where model_id = %(m)s",
            {"m": model_id},
        )
        is None
    ):
        raise NotFoundError(f"model {model_id} not found")


def near_miss(
    pool: DictRowPool,
    model_id: int,
    parameter: str,
    width: Optional[str] = None,
    split_kind: str = "test",
    as_of_date: Optional[DateLike] = None,
) -> list[dict[str, Any]]:
    """Do the rows just below the cut differ from the rows inside it?

    Reads ``triage.near_miss`` (migration 0037) on each scored date: the ``selected``
    band (rank ≤ k), the ``near_miss`` band (k < rank ≤ k + w) and the ``rest``, where k
    and w are ``resolve_k`` of the cut and the width over the date's scored rows.

    Args:
        pool: the project database.
        model_id: the model.
        parameter: the top-k cut, e.g. ``'100_abs'``.
        width: the near-miss band's size as a cut; ``None`` is the cut itself.
        split_kind: the prediction split.
        as_of_date: one scored date; ``None`` reads every one.

    Returns:
        Rows ``as_of_date, band, first_rank, last_rank, n, n_labeled, n_positive,
        base_rate, mean_outcome, mean_score``, three per date in band order. The rates
        count labeled rows, positive meaning ``outcome > 0``; ``mean_score`` reads every
        row of the band. A band with no row has ``None`` ranks and rates. Empty when the
        model scored nothing on the split.

    Raises:
        NotFoundError: no model has this id.
    """
    _require_model(pool, model_id)
    return _rows(
        pool,
        "select d.as_of_date, nm.band, nm.first_rank, nm.last_rank, nm.n, nm.n_labeled,"
        "       nm.n_positive, nm.base_rate, nm.mean_outcome, nm.mean_score"
        " from (select distinct p.as_of_date from triage.predictions p"
        "        where p.model_id = %(m)s"
        "          and p.split_kind = cast(%(split)s as triage.split_kind)"
        "          and (%(d)s::date is null or p.as_of_date = %(d)s::date)) d"
        " cross join lateral triage.near_miss(%(m)s, cast(%(split)s as triage.split_kind),"
        "                                     d.as_of_date, %(p)s, %(w)s) nm"
        " order by d.as_of_date, nm.band_ix",
        {
            "m": model_id,
            "split": split_kind,
            "d": None if as_of_date is None else str(as_of_date),
            "p": parameter,
            "w": width,
        },
    )


#: The classes of a labeled row at a top-k cut, in the order a reader lists them.
PROTOTYPE_CLASSES = ("tp", "fp", "fn", "tn")


def prototypes(
    pool: DictRowPool,
    model_id: int,
    parameter: Optional[str] = None,
    as_of_date: Optional[DateLike] = None,
    split_kind: str = "test",
) -> list[dict[str, Any]]:
    """The most typical entities of each class at a cut, as persisted.

    Reads ``triage.prototypes`` (written by ``triage postmodel prototypes``).

    Args:
        pool: the project database.
        model_id: the model.
        parameter: the top-k cut; ``None`` reads every cut persisted.
        as_of_date: one prediction date; ``None`` reads every date.
        split_kind: the prediction split.

    Returns:
        Rows ``as_of_date, parameter, class, class_size, rank, entity_id, distance,
        deviations, computed_at``, by date, cut, class (tp, fp, fn, tn) and rank.
        ``deviations`` lists the entity's largest departures from its class centroid,
        each ``feature, value, z, centroid_z, deviation`` in z units. Empty when nothing
        is persisted.

    Raises:
        NotFoundError: no model has this id.
    """
    _require_model(pool, model_id)
    return _rows(
        pool,
        "select as_of_date, parameter, class, class_size, rank, entity_id, distance,"
        "       deviations, computed_at"
        " from triage.prototypes"
        " where model_id = %(m)s and split_kind = cast(%(split)s as triage.split_kind)"
        "   and (%(p)s::text is null or parameter = %(p)s)"
        "   and (%(d)s::date is null or as_of_date = %(d)s::date)"
        " order by as_of_date, parameter,"
        "          array_position(array['tp', 'fp', 'fn', 'tn'], class), rank",
        {
            "m": model_id,
            "split": split_kind,
            "p": parameter,
            "d": None if as_of_date is None else str(as_of_date),
        },
    )


# ------------------------------------------------------ calibration, score distribution


def _check_bins(bins: int) -> None:
    if bins < 1:
        raise ValueError(f"bins={bins} holds no rows — pass 1 or more")


def calibration(
    pool: DictRowPool,
    model_id: int,
    split_kind: str = "test",
    as_of_date: Optional[DateLike] = None,
    bins: int = 10,
) -> Optional[dict[str, Any]]:
    """Does a model's score match the outcome its rows realize? Deciles of one date.

    Reads ``triage.monitoring_calibration`` over the labeled rows of one evaluated date,
    at the model's training label timespan, and ``triage.score_ties`` over the scored
    rows of that date. The decile is ``ceil(cume_dist · bins)`` over the score order, so
    tied scores share a decile and a decile that a block of ties covers is absent.

    Args:
        pool: the project database.
        model_id: the model.
        split_kind: the prediction split.
        as_of_date: the date; ``None`` reads the model's latest evaluated date of the
            split (over everyone, not a Subset).
        bins: the number of score buckets.

    Returns:
        ``None`` when the model has no evaluated date or no training label timespan.
        Otherwise ``as_of_date``, ``label_timespan``, ``problem_type``, ``bins``,
        ``deciles`` (rows ``decile, n, avg_score, mean_outcome, lift, realized_rate,
        n_distinct_scores``; ``realized_rate`` is ``None`` unless the model is a
        classifier) and ``ties`` (``n, n_distinct, largest_tie_n, largest_tie_score,
        largest_tie_share``).

    Raises:
        NotFoundError: no model has this id.
        ValueError: ``bins`` is below 1.
    """
    _check_bins(bins)
    ctx = _one(
        pool,
        "select m.training_label_timespan::text as label_timespan,"
        "       triage.model_problem_type(m.model_id)::text as problem_type,"
        "       coalesce(cast(%(date)s as date),"
        "                (select max(e.as_of_date) from triage.evaluations e"
        "                  where e.model_id = m.model_id"
        "                    and e.split_kind = cast(%(split)s as triage.split_kind)"
        "                    and e.subset_hash = '')) as as_of_date"
        " from triage.models m where m.model_id = %(m)s",
        {
            "m": model_id,
            "split": split_kind,
            "date": None if as_of_date is None else str(as_of_date),
        },
    )
    if ctx is None:
        raise NotFoundError(f"model {model_id} not found")
    if ctx["as_of_date"] is None or ctx["label_timespan"] is None:
        return None
    params = {
        "m": model_id,
        "split": split_kind,
        "date": str(ctx["as_of_date"]),
        "ts": ctx["label_timespan"],
        "bins": bins,
    }
    deciles = _rows(
        pool,
        "select decile, n, avg_score, mean_outcome, lift, realized_rate,"
        "       n_distinct_scores"
        " from triage.monitoring_calibration(%(m)s, cast(%(split)s as triage.split_kind),"
        "        cast(%(date)s as date), cast(%(ts)s as interval), '', %(bins)s)"
        " order by decile",
        params,
    )
    ties = _one(
        pool,
        "select n, n_distinct, largest_tie_n, largest_tie_score, largest_tie_share"
        " from triage.score_ties(%(m)s, cast(%(split)s as triage.split_kind),"
        "        cast(%(date)s as date))",
        params,
    )
    return {
        "as_of_date": ctx["as_of_date"],
        "label_timespan": ctx["label_timespan"],
        "problem_type": ctx["problem_type"],
        "bins": bins,
        "deciles": deciles,
        "ties": ties,
    }


def score_histogram(
    pool: DictRowPool, model_id: int, bins: int = 20
) -> list[dict[str, Any]]:
    """How a model's test scores spread, and what the rows of each score bucket realized.

    Reads ``triage.model_score_histogram`` over every test date. A classifier whose
    scores all lie in ``[0, 1]`` is binned over ``[0, 1]``; any other model over the range
    of its own scores.

    Args:
        pool: the project database.
        model_id: the model.
        bins: the number of equal-width buckets.

    Returns:
        Rows ``bin, lo, hi, n, n_pos, mean_outcome, n_event, n_censored``, ordered by
        bin; empty bins are absent. ``n`` counts every scored row, labeled or not.
        ``n_pos`` (the positives) is ``None`` unless the model is a classifier;
        ``mean_outcome`` averages the labeled rows of the bin. ``n_event`` and
        ``n_censored`` count a survival model's rows with an observed event and its
        censored rows (migration 0036), and are ``None`` for the other types.

    Raises:
        ValueError: ``bins`` is below 1.
    """
    _check_bins(bins)
    return _rows(
        pool,
        "select bin, lo, hi, n, n_pos, mean_outcome, n_event, n_censored"
        " from triage.model_score_histogram(%(m)s, %(bins)s) order by bin",
        {"m": model_id, "bins": bins},
    )


# ------------------------------------------------------------- leaderboard, model groups


def leaderboard(
    pool: DictRowPool,
    experiment_hash: str,
    *,
    metric: Optional[str] = None,
    parameter: Optional[str] = None,
    limit: int = 20,
    windowed: bool = False,
) -> list[dict[str, Any]]:
    """An experiment's leaderboard rows, best first within each metric and parameter.

    Args:
        pool: the project database.
        experiment_hash: the full experiment hash.
        metric: keep one metric; ``None`` keeps all.
        parameter: keep one metric parameter; ``None`` keeps all.
        limit: the most rows to return.
        windowed: read ``triage.evaluations_windowed`` (one row per model across its whole
            test window, full cohort only) instead of the per-date ``triage.leaderboard``.

    Returns:
        Per date: ``model_group_id, model_type, split_kind, metric, parameter, as_of_date,
        value, value_expected, value_std, model_id, train_end_time, coverage``. Windowed:
        ``model_group_id, model_type, split_kind, metric, parameter, n_as_of_dates,
        value_mean, value_stddev, value_min, model_id, train_end_time, coverage``.
        ``coverage`` is the group's (:func:`coverage`).

    Raises:
        psycopg.errors.ObjectNotInPrerequisiteState: the per-date matview was never
            refreshed. The caller decides whether to refresh it.
    """
    if windowed:
        # A plain view over per-date rows, so no matview. Full cohort only (subset_hash =
        # ''): a window pooled across subsets answers no question anyone asks.
        sql: LiteralString = (
            "select mg.model_group_id, mg.model_type, w.split_kind, w.metric,"
            "       w.parameter, w.n_as_of_dates, w.value_mean, w.value_stddev,"
            "       w.value_min, w.model_id, m.train_end_time, c.coverage"
            " from triage.evaluations_windowed w"
            " join triage.models m on m.model_id = w.model_id"
            " join triage.model_groups mg on mg.model_group_id = m.model_group_id"
            " join triage.runs r on r.run_id = m.run_id"
            " left join triage.model_group_coverage(%(hash)s) c"
            "        on c.model_group_id = mg.model_group_id"
            " where r.experiment_hash = %(hash)s and w.subset_hash = ''"
        )
    else:
        sql = (
            "select l.model_group_id, l.model_type, l.split_kind, l.metric, l.parameter,"
            "       l.as_of_date, l.value, l.value_expected, l.value_std, l.model_id,"
            "       l.train_end_time, c.coverage"
            " from triage.leaderboard l"
            " left join triage.model_group_coverage(%(hash)s) c"
            "        on c.model_group_id = l.model_group_id"
            " where l.experiment_hash = %(hash)s"
        )
    params: dict[str, Any] = {"hash": experiment_hash, "limit": limit}
    t: LiteralString = "w" if windowed else "l"
    if metric:
        sql += " and " + t + ".metric = %(metric)s"
        params["metric"] = metric
    if parameter is not None:
        sql += " and " + t + ".parameter = %(parameter)s"
        params["parameter"] = parameter
    # Best first: a score descending, a loss ascending, as triage.higher_is_better
    # (and a project's triage.metric_directions rows) says.
    if windowed:
        sql += (
            " order by w.metric, w.parameter,"
            " case when triage.higher_is_better(w.metric) then w.value_mean"
            " else -w.value_mean end desc nulls last"
        )
    else:
        sql += (
            " order by l.metric, l.parameter, l.as_of_date desc,"
            " case when triage.higher_is_better(l.metric) then l.value"
            " else -l.value end desc nulls last"
        )
    sql += " limit %(limit)s"
    return _rows(pool, sql, params)


#: Coverage spread across groups above which a comparison is told to use the common rows.
COVERAGE_SPREAD = 0.01


def coverage(pool: DictRowPool, experiment_hash: str) -> list[dict[str, Any]]:
    """What share of the labeled rows each model group of an experiment scored.

    A model that cannot forecast a row (a baseline with no target history) abstains and
    leaves no prediction, so groups can be evaluated on different rows. Coverage is
    ``scored labeled rows / labeled rows`` on each test date, averaged over the group's
    model-dates.

    Returns:
        Rows ``model_group_id, n_dates, coverage, min_coverage``, ordered by group.
    """
    return _rows(
        pool,
        "select model_group_id, n_dates, coverage, min_coverage"
        " from triage.model_group_coverage(%(h)s) order by model_group_id",
        {"h": experiment_hash},
    )


def coverage_differs(values: Iterable[Optional[float]]) -> bool:
    """True when the groups' coverage spans more than :data:`COVERAGE_SPREAD`."""
    defined = [v for v in values if v is not None]
    return bool(defined) and max(defined) - min(defined) > COVERAGE_SPREAD


def common_rows(
    pool: DictRowPool,
    model_group_ids: Iterable[int],
    split_kind: str = "test",
    as_of_date: Optional[DateLike] = None,
) -> list[dict[str, Any]]:
    """Each group's errors on its own rows and on the rows every listed group scored.

    Reads ``triage.common_rows_metrics``: the common rows of a date are the labeled rows that
    every listed group scored on it, so a date some group did not score has none.

    Returns:
        Rows ``model_group_id, model_id, as_of_date, n_labeled, n_scored_own, n_common,
        mae_own, mae_common, rmse_common, wape_common, bias_common``, by date then group.

    Raises:
        psycopg.errors.RaiseException: a group has two models scoring one date.
    """
    return _rows(
        pool,
        "select model_group_id, model_id, as_of_date, n_labeled, n_scored_own, n_common,"
        "       mae_own, mae_common, rmse_common, wape_common, bias_common"
        " from triage.common_rows_metrics(%(g)s, cast(%(s)s as triage.split_kind),"
        "        %(d)s::date)",
        {
            "g": sorted(set(model_group_ids)),
            "s": split_kind,
            "d": None if as_of_date is None else str(as_of_date),
        },
    )


def default_metric(
    pool: DictRowPool, experiment_hash: str, metric: Optional[str] = None
) -> Optional[tuple[str, str]]:
    """The experiment's most-evaluated ``(metric, parameter)`` in ``triage.audition``.

    Args:
        pool: the project database.
        experiment_hash: the full experiment hash.
        metric: restrict the choice to this metric's parameters.

    Returns:
        ``(metric, parameter)``, or ``None`` when no test split has been evaluated.
    """
    row = _one(
        pool,
        "select metric, parameter, sum(n_splits_evaluated) as n"
        " from triage.audition where experiment_hash = %(h)s"
        "   and (%(m)s::text is null or metric = %(m)s)"
        " group by metric, parameter order by n desc, metric, parameter limit 1",
        {"h": experiment_hash, "m": metric},
    )
    return None if row is None else (row["metric"], row["parameter"])


def model_groups(
    pool: DictRowPool, experiment_hash: str, metric: str, parameter: str
) -> list[dict[str, Any]]:
    """An experiment's model groups ranked by average distance from the best, on one metric.

    Args:
        pool: the project database.
        experiment_hash: the full experiment hash.
        metric: the audition metric.
        parameter: the metric parameter.

    Returns:
        Rows ``model_group_id, model_type, n_models, avg_value, stddev_value, max_regret,
        avg_duration_ms, coverage, min_coverage``. A group with no evaluation on the metric
        has ``None`` values. Coverage is :func:`coverage`'s.
    """
    return _rows(
        pool,
        "select mgs.model_group_id, mgs.model_type, mgs.n_models,"
        "       a.avg_value, a.stddev_value, a.max_regret,"
        "       (select avg(mm.train_duration_ms)::bigint from triage.models mm"
        "          join triage.runs rr on rr.run_id = mm.run_id"
        "         where mm.model_group_id = mgs.model_group_id"
        "           and rr.experiment_hash = %(h)s) as avg_duration_ms,"
        "       c.coverage, c.min_coverage"
        " from triage.model_group_summary mgs"
        " left join triage.model_group_coverage(%(h)s) c"
        "        on c.model_group_id = mgs.model_group_id"
        " left join triage.audition a"
        "        on a.experiment_hash = mgs.experiment_hash"
        "       and a.model_group_id = mgs.model_group_id"
        "       and a.metric = %(m)s and a.parameter = %(p)s"
        " where mgs.experiment_hash = %(h)s"
        " order by a.avg_distance_from_best asc nulls last, mgs.model_group_id",
        {"h": experiment_hash, "m": metric, "p": parameter},
    )


# ------------------------------------------------------------------ regression residuals

#: The segment cuts of a regression model's rows, besides a Subset (``subset:NAME``).
RESIDUAL_CUTS = ("band", "sbc", "cold-start")

#: The most forecast-actual pairs :func:`forecast_actual` returns.
FORECAST_ACTUAL_CAP = 200_000


def residual_summary(
    pool: DictRowPool, model_id: int, split_kind: str = "test"
) -> list[dict[str, Any]]:
    """Per date: a regression model's errors, their concentration and its forecast shape.

    Reads ``triage.regression_residual_summary`` over the labeled rows of each scored date.

    Returns:
        Rows ``as_of_date, n, sum_outcome, mae, rmse, wape, bias, top100_error_share,
        top1000_error_share, top1pct_error_share, negative_share, n_distinct_scores,
        mode_score, mode_share, min_score, max_score, coverage_point``, by date. ``bias`` is
        the mean of forecast − outcome; ``coverage_point`` is the share of rows with
        outcome ≤ forecast; a ``topN_error_share`` is the share of the absolute error held
        by the N largest errors (all of it when the date has N rows or fewer).
    """
    return _rows(
        pool,
        "select as_of_date, n, sum_outcome, mae, rmse, wape, bias, top100_error_share,"
        "       top1000_error_share, top1pct_error_share, negative_share,"
        "       n_distinct_scores, mode_score, mode_share, min_score, max_score,"
        "       coverage_point"
        " from triage.regression_residual_summary(%(m)s,"
        "        cast(%(s)s as triage.split_kind))",
        {"m": model_id, "s": split_kind},
    )


def _segment_cut(pool: DictRowPool, cut: str) -> tuple[str, Optional[str]]:
    """``cut`` as ``regression_residuals_by_segment`` takes it, and the Subset's name."""
    if cut in RESIDUAL_CUTS:
        return cut.replace("-", "_"), None
    if cut.startswith("subset:") and cut[len("subset:") :]:
        return resolve_subset(pool, cut[len("subset:") :])
    raise ValueError(
        f"{cut!r} is not a segment cut — expected {' | '.join(RESIDUAL_CUTS)}"
        " | subset:NAME"
    )


def residuals_by_segment(
    pool: DictRowPool,
    model_id: int,
    cut: str = "band",
    split_kind: str = "test",
    pooled: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """A regression model's errors per segment of its rows.

    Segments come from the entity's target history before each date
    (``triage.prior_label_history``): ``band`` (the band of its mean label), ``sbc`` (its
    Syntetos–Boylan class), ``cold-start`` (no prior label against some), or
    ``subset:NAME`` (in the Subset against outside it).

    Args:
        pool: the project database.
        model_id: the model.
        cut: ``band``, ``sbc``, ``cold-start`` or ``subset:NAME``.
        split_kind: the prediction split.
        pooled: ``True`` keeps the rows pooled over the dates, ``False`` the per-date rows,
            ``None`` both.

    Returns:
        Rows ``as_of_date, segment, segment_order, n, entities, sum_outcome,
        sum_forecast, mae, wape, bias, share_of_abs_error``. ``as_of_date`` is ``None`` on
        a pooled row. ``share_of_abs_error`` is the segment's share of its scope's absolute
        error. Pooled rows come first, then by date, each in segment order.

    Raises:
        NotFoundError: ``subset:NAME`` names no Subset.
        ValueError: ``cut`` is none of the above, or names two Subsets.
    """
    sql_cut, _name = _segment_cut(pool, cut)
    return _rows(
        pool,
        "select as_of_date, segment, segment_order, n, entities, sum_outcome,"
        "       sum_forecast, mae, wape, bias, share_of_abs_error"
        " from triage.regression_residuals_by_segment(%(m)s,"
        "        cast(%(s)s as triage.split_kind), %(c)s)"
        " where %(p)s::boolean is null or (as_of_date is null) = %(p)s::boolean",
        {"m": model_id, "s": split_kind, "c": sql_cut, "p": pooled},
    )


def largest_errors(
    pool: DictRowPool, model_id: int, n: int = 20, split_kind: str = "test"
) -> list[dict[str, Any]]:
    """The ``n`` rows with the largest absolute error, over every date of the split.

    Returns:
        Rows ``entity_id, as_of_date, forecast, outcome, error, abs_error, label``,
        largest first. ``error`` is forecast − outcome. ``label`` is the entity's
        ``name``, ``label``, ``display_name`` or ``title`` from ``entity_attributes``,
        when its source has one.

    Raises:
        ValueError: ``n`` is below 1.
    """
    if n < 1:
        raise ValueError(f"n={n} lists no rows — pass 1 or more")
    return _rows(
        pool,
        "select entity_id, as_of_date, forecast, outcome, error, abs_error, label"
        " from triage.largest_errors(%(m)s, cast(%(s)s as triage.split_kind), %(n)s)",
        {"m": model_id, "s": split_kind, "n": n},
    )


def forecast_actual(
    pool: DictRowPool,
    model_id: int,
    split_kind: str = "test",
    cap: int = FORECAST_ACTUAL_CAP,
) -> dict[str, Any]:
    """Forecast and outcome pairs of a model's labeled rows, at most ``cap`` of them.

    Over the cap, the pairs kept are those of the entities that come first in the order of
    ``hashint8(entity_id)``: a sample by entity that is the same on every call.

    Returns:
        ``n_total`` (the labeled rows), ``sampled`` (whether the cap cut them) and
        ``pairs``, rows ``entity_id, as_of_date, forecast, outcome``.

    Raises:
        ValueError: ``cap`` is below 1.
    """
    if cap < 1:
        raise ValueError(f"cap={cap} keeps no pairs — pass 1 or more")
    params = {"m": model_id, "s": split_kind, "cap": cap}
    total = _one(
        pool,
        "select count(*) as n from triage.scored_labels(%(m)s,"
        "        cast(%(s)s as triage.split_kind))"
        " where outcome is not null and score is not null",
        params,
    )
    pairs = _rows(
        pool,
        "select entity_id, as_of_date, score as forecast, outcome"
        " from triage.scored_labels(%(m)s, cast(%(s)s as triage.split_kind))"
        " where outcome is not null and score is not null"
        " order by hashint8(entity_id), entity_id, as_of_date limit %(cap)s",
        params,
    )
    n_total = 0 if total is None else total["n"]
    return {"n_total": n_total, "sampled": n_total > len(pairs), "pairs": pairs}


def gains_curve(
    pool: DictRowPool,
    model_id: int,
    split_kind: str = "test",
    as_of_date: Optional[DateLike] = None,
    points: int = 100,
) -> Optional[dict[str, Any]]:
    """The cumulative-gains curve of one scored date: the share of the outcome the top k hold.

    Reads ``triage.gains_curve`` (migration 0035): ``points + 1`` cuts of the population
    ranking, from k = 0 to every scored row. ``captured`` and ``lift`` are NULL on a date
    whose labeled outcome is negative somewhere or sums to 0.

    Args:
        pool: the project database.
        model_id: the model.
        split_kind: the prediction split.
        as_of_date: the date; ``None`` reads the model's latest evaluated date of the split
            (over everyone, not a Subset).
        points: the cuts after k = 0.

    Returns:
        ``None`` when the model has no evaluated date. Otherwise ``as_of_date``,
        ``problem_type``, ``points``, ``aucc`` (the stored value, or ``None``) and ``rows``
        (``k_pct, k, n_labeled_topk, captured, lift, mean_outcome_topk``).

    Raises:
        NotFoundError: no model has this id.
        ValueError: ``points`` is below 1.
    """
    if points < 1:
        raise ValueError(f"points={points} draws no curve — pass 1 or more")
    ctx = _one(
        pool,
        "select triage.model_problem_type(m.model_id)::text as problem_type,"
        "       coalesce(cast(%(date)s as date),"
        "                (select max(e.as_of_date) from triage.evaluations e"
        "                  where e.model_id = m.model_id"
        "                    and e.split_kind = cast(%(split)s as triage.split_kind)"
        "                    and e.subset_hash = '')) as as_of_date"
        " from triage.models m where m.model_id = %(m)s",
        {
            "m": model_id,
            "split": split_kind,
            "date": None if as_of_date is None else str(as_of_date),
        },
    )
    if ctx is None:
        raise NotFoundError(f"model {model_id} not found")
    if ctx["as_of_date"] is None:
        return None
    params = {
        "m": model_id,
        "split": split_kind,
        "date": str(ctx["as_of_date"]),
        "points": points,
    }
    rows = _rows(
        pool,
        "select k_pct, k, n_labeled_topk, captured, lift, mean_outcome_topk"
        " from triage.gains_curve(%(m)s, cast(%(split)s as triage.split_kind),"
        "                         cast(%(date)s as date), %(points)s)",
        params,
    )
    aucc = _one(
        pool,
        "select value from triage.evaluations where model_id = %(m)s"
        " and split_kind = cast(%(split)s as triage.split_kind)"
        " and as_of_date = cast(%(date)s as date) and subset_hash = ''"
        " and metric = 'aucc' and parameter = ''",
        params,
    )
    return {
        "as_of_date": ctx["as_of_date"],
        "problem_type": ctx["problem_type"],
        "points": points,
        "aucc": None if aucc is None else aucc["value"],
        "rows": rows,
    }


# ------------------------------------------------------------------- survival diagnostics

#: The kinds of row ``triage.survival_diagnostics`` holds (migration 0036).
SURVIVAL_KINDS = (
    "km_decile",
    "km_group",
    "logrank",
    "calibration",
    "brier",
    "auc_t",
    "uno_c",
)


def survival_diagnostics(
    pool: DictRowPool,
    model_id: int,
    horizon: Optional[float] = None,
    as_of_date: Optional[DateLike] = None,
    split_kind: str = "test",
) -> Optional[dict[str, Any]]:
    """The persisted survival set of one model, date and horizon.

    Reads ``triage.survival_diagnostics`` (written by ``triage postmodel survival``).

    Args:
        pool: the project database.
        model_id: the model.
        horizon: the horizon; ``None`` reads the one computed most recently.
        as_of_date: the date; ``None`` reads the latest date computed at the horizon.
        split_kind: the prediction split.

    Returns:
        ``None`` when nothing is persisted at that horizon and date. Otherwise
        ``as_of_date``, ``horizon``, ``computed_at`` and ``kinds``: for each kind of
        :data:`SURVIVAL_KINDS` its rows ``bin, t, value, n, extra``, ordered by bin and
        t (an absent kind has no rows). ``value`` is ``None`` where the kind is undefined,
        and ``extra['undefined']`` then says why.

    Raises:
        NotFoundError: no model has this id.
    """
    if (
        _one(
            pool,
            "select 1 as found from triage.models where model_id = %(m)s",
            {"m": model_id},
        )
        is None
    ):
        raise NotFoundError(f"model {model_id} not found")
    params: dict[str, Any] = {
        "m": model_id,
        "split": split_kind,
        "h": horizon,
        "date": None if as_of_date is None else str(as_of_date),
    }
    ctx = _one(
        pool,
        "with pick as ("
        "    select coalesce(%(h)s::double precision,"
        "                    (select sd.horizon from triage.survival_diagnostics sd"
        "                      where sd.model_id = %(m)s"
        "                        and sd.split_kind = cast(%(split)s as triage.split_kind)"
        "                      order by sd.computed_at desc, sd.horizon limit 1))"
        "           as horizon"
        ")"
        " select p.horizon,"
        "        coalesce(cast(%(date)s as date),"
        "                 (select max(sd.as_of_date) from triage.survival_diagnostics sd"
        "                   where sd.model_id = %(m)s"
        "                     and sd.split_kind = cast(%(split)s as triage.split_kind)"
        "                     and sd.horizon = p.horizon)) as as_of_date"
        " from pick p",
        params,
    )
    if ctx is None or ctx["horizon"] is None or ctx["as_of_date"] is None:
        return None
    params.update(h=ctx["horizon"], date=str(ctx["as_of_date"]))
    rows = _rows(
        pool,
        "select kind, bin, t, value, n, extra, computed_at"
        " from triage.survival_diagnostics"
        " where model_id = %(m)s and split_kind = cast(%(split)s as triage.split_kind)"
        "   and as_of_date = cast(%(date)s as date) and horizon = %(h)s"
        " order by kind, bin, t",
        params,
    )
    if not rows:
        return None
    kinds: dict[str, list[dict[str, Any]]] = {k: [] for k in SURVIVAL_KINDS}
    for r in rows:
        kinds[r.pop("kind")].append(r)
    computed_at = max(r.pop("computed_at") for group in kinds.values() for r in group)
    return {
        "as_of_date": ctx["as_of_date"],
        "horizon": ctx["horizon"],
        "computed_at": computed_at,
        "kinds": kinds,
    }
