"""Where a regression model group beats its baseline: per segment, per entity, over time.

The two groups are compared on the rows both scored: for each test date, the model of each
group that scored it, joined on ``(entity_id, as_of_date)`` over the labeled rows. Computed
on read from ``triage.scored_labels`` and ``triage.prior_label_history`` (migration 0033).
Nothing is written.

* **Segments** — the target-history ``band``, ``sbc`` class and ``cold_start`` flag of each
  row, and every Subset the Experiment was evaluated on. Per segment: both groups' MAE, WAPE
  and bias, whether the model wins (lower MAE), the share of rows where it is closer, and
  its excess absolute error over the baseline.
* **Entities** — each entity's absolute error pooled over the test dates. The share of
  entities the model wins, of ties, of the outcome total held by the won entities, and the
  median ratio of the model's error to the baseline's. An entity-group query repeats this
  at a coarser level.
* **Chance** — on the entities scored on every shared test date: how many dates each one
  is won, against the Poisson-binomial count expected if wins fell at each date's win share
  independently of the entity. A chi-square test, cells merged until each expects 5 or more.
  An entity that wins on one date is no likelier to win on the next if the test holds.
* **Out of time** — the entities the model won over the earlier dates, scored on the last.

"Closer" is strict: a row where both errors are equal is a tie and is not won.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, LiteralString, Optional, cast

import numpy as np
import polars as pl

from triage.problem_types import REGRESSION_TYPES
from triage.util.db import DictRowPool

#: Model classes under this prefix are baselines.
BASELINE_PREFIX = "triage.component.catwalk.baselines."

#: The history cuts every comparison reads, besides the Subsets.
HISTORY_CUTS = ("band", "sbc", "cold_start")

#: The smallest expected count a chi-square cell may hold.
MIN_EXPECTED = 5.0


class ComparisonError(ValueError):
    """The two groups cannot be compared: no shared rows, a wrong type, an unknown group."""


@dataclass
class Comparison:
    """A model group against a baseline group on the rows both scored."""

    experiment_hash: str
    model_group_id: int
    baseline_group_id: int
    baseline_chosen: bool
    dates: list[date]
    n_rows: int
    segments: list[dict[str, Any]] = field(default_factory=list)
    segment_win_share: Optional[float] = None
    entities: dict[str, Any] = field(default_factory=dict)
    entity_groups: Optional[dict[str, Any]] = None
    chance: Optional[dict[str, Any]] = None
    out_of_time: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------------------- reads


def group_experiment(
    pool: DictRowPool, model_group_id: int, experiment_hash: Optional[str] = None
) -> str:
    """The Experiment a group's models belong to: ``experiment_hash``, else the only one.

    Raises:
        ComparisonError: the group has no model, none in ``experiment_hash``, or models in
            several Experiments and none was named.
    """
    with pool.connection() as conn:
        found = [
            r["experiment_hash"]
            for r in conn.execute(
                "select distinct r.experiment_hash from triage.models m"
                " join triage.runs r on r.run_id = m.run_id"
                " where m.model_group_id = %(g)s order by 1",
                {"g": model_group_id},
            ).fetchall()
        ]
    if experiment_hash is not None:
        matches = [h for h in found if h.startswith(experiment_hash)]
        if len(matches) != 1:
            raise ComparisonError(
                f"model group {model_group_id} has no model in experiment"
                f" {experiment_hash!r} — its experiments: {', '.join(found) or 'none'}"
            )
        return matches[0]
    if not found:
        raise ComparisonError(
            f"model group {model_group_id} has no model with run lineage — list a"
            " project's groups with: triage models <experiment>"
        )
    if len(found) > 1:
        raise ComparisonError(
            f"model group {model_group_id} has models in {len(found)} experiments"
            f" ({', '.join(h[:12] for h in found)}) — pass --experiment"
        )
    return found[0]


def _check_regression(pool: DictRowPool, experiment_hash: str) -> None:
    with pool.connection() as conn:
        row = conn.execute(
            "select problem_type::text as t from triage.experiments"
            " where experiment_hash = %(h)s",
            {"h": experiment_hash},
        ).fetchone()
    problem_type = None if row is None else row["t"]
    if problem_type not in REGRESSION_TYPES:
        raise ComparisonError(
            f"experiment {experiment_hash[:12]} is {problem_type}: the baseline"
            " comparison reads regression and regression_ranking models. For a"
            " classifier compare lists with: triage postmodel compare MODEL_A MODEL_B"
        )


def default_baseline(
    pool: DictRowPool, experiment_hash: str, model_group_id: int
) -> int:
    """The baseline group with the lowest MAE on the rows it shares with the model group.

    A baseline is a group whose class lives under :data:`BASELINE_PREFIX`. Each candidate's
    MAE is averaged over the test dates on the rows it and the model group both scored.

    Raises:
        ComparisonError: the Experiment has no baseline group with a shared row.
    """
    with pool.connection() as conn:
        candidates = [
            r["model_group_id"]
            for r in conn.execute(
                "select distinct mg.model_group_id from triage.model_groups mg"
                " join triage.models m on m.model_group_id = mg.model_group_id"
                " join triage.runs r on r.run_id = m.run_id"
                " where r.experiment_hash = %(h)s and mg.model_group_id <> %(g)s"
                "   and starts_with(mg.model_type, %(p)s) order by 1",
                {"h": experiment_hash, "g": model_group_id, "p": BASELINE_PREFIX},
            ).fetchall()
        ]
        best: Optional[tuple[float, int]] = None
        for candidate in candidates:
            row = conn.execute(
                "select avg(mae_common) as mae from triage.common_rows_metrics("
                "        %(gs)s, 'test') where model_group_id = %(c)s",
                {"gs": [model_group_id, candidate], "c": candidate},
            ).fetchone()
            mae = None if row is None else row["mae"]
            if mae is not None and (best is None or (mae, candidate) < best):
                best = (mae, candidate)
    if best is None:
        raise ComparisonError(
            f"experiment {experiment_hash[:12]} has no baseline group ({BASELINE_PREFIX}*)"
            f" that shares a scored row with group {model_group_id} — pass --baseline GROUP"
        )
    return best[1]


def _scored(pool: DictRowPool, experiment_hash: str, group_id: int) -> pl.DataFrame:
    """One group's labeled scored rows in the Experiment, with each row's history segments."""
    with pool.connection() as conn:
        rows = conn.execute(
            # each set-returning function runs once per model, not once per row
            "with ms as materialized ("
            "    select m.model_id from triage.models m"
            "    join triage.runs r on r.run_id = m.run_id"
            "    where m.model_group_id = %(g)s and r.experiment_hash = %(h)s),"
            " s as materialized ("
            "    select ms.model_id, s.* from ms"
            "    cross join lateral triage.scored_labels(ms.model_id, 'test') s"
            "    where s.outcome is not null and s.score is not null),"
            " h as materialized ("
            "    select ms.model_id, h.* from ms"
            "    cross join lateral triage.prior_label_history(ms.model_id, 'test') h)"
            " select s.model_id, s.entity_id, s.as_of_date, s.score, s.outcome,"
            "        h.band, h.band_ix, h.sbc_class, h.cold_start"
            " from s left join h on h.model_id = s.model_id"
            "  and h.entity_id = s.entity_id and h.as_of_date = s.as_of_date",
            {"g": group_id, "h": experiment_hash},
        ).fetchall()
    frame = pl.DataFrame(
        rows,
        schema={
            "model_id": pl.Int64,
            "entity_id": pl.Int64,
            "as_of_date": pl.Date,
            "score": pl.Float64,
            "outcome": pl.Float64,
            "band": pl.Utf8,
            "band_ix": pl.Int64,
            "sbc_class": pl.Utf8,
            "cold_start": pl.Boolean,
        },
        orient="row",
    )
    twins = frame.group_by("as_of_date").agg(pl.col("model_id").n_unique().alias("n"))
    twins = twins.filter(pl.col("n") > 1)
    if twins.height:
        raise ComparisonError(
            f"model group {group_id} has {twins['n'][0]} models scoring"
            f" {twins['as_of_date'][0]} — compare one model per group and date"
        )
    return frame


def _subsets(pool: DictRowPool, experiment_hash: str) -> list[tuple[str, str]]:
    """``(subset_hash, name)`` of every user Subset the Experiment's models were evaluated on."""
    with pool.connection() as conn:
        return [
            (r["subset_hash"], r["name"] or r["subset_hash"][:12])
            for r in conn.execute(
                "select distinct s.subset_hash, s.config->>'name' as name"
                " from triage.evaluations e"
                " join triage.models m on m.model_id = e.model_id"
                " join triage.runs r on r.run_id = m.run_id"
                " join triage.subsets s on s.subset_hash = e.subset_hash"
                " where r.experiment_hash = %(h)s and e.subset_hash <> ''"
                "   and not starts_with(e.subset_hash, 'common:') order by 2, 1",
                {"h": experiment_hash},
            ).fetchall()
        ]


def _members(pool: DictRowPool, subset_hash: str) -> pl.DataFrame:
    with pool.connection() as conn:
        rows = conn.execute(
            "select entity_id, as_of_date from triage.subset_members"
            " where subset_hash = %(s)s",
            {"s": subset_hash},
        ).fetchall()
    return pl.DataFrame(
        rows, schema={"entity_id": pl.Int64, "as_of_date": pl.Date}, orient="row"
    ).with_columns(pl.lit(True).alias("member"))


def entity_groups_from_query(pool: DictRowPool, sql: str) -> pl.DataFrame:
    """The ``entity_id, group_key`` rows of a user's SQL file.

    Raises:
        ComparisonError: the query does not return both columns, or maps an entity twice.
    """
    with pool.connection() as conn:
        # the user's own SQL file, run as written — like a cohort or label query
        cur = conn.execute(cast(LiteralString, sql))
        columns = [c.name for c in cur.description or []]
        if not {"entity_id", "group_key"} <= set(columns):
            raise ComparisonError(
                f"the entity-group query returns {columns}; it must return entity_id"
                " and group_key"
            )
        rows = [(r["entity_id"], str(r["group_key"])) for r in cur.fetchall()]
    frame = pl.DataFrame(
        rows, schema={"entity_id": pl.Int64, "group_key": pl.Utf8}, orient="row"
    )
    if frame["entity_id"].n_unique() != frame.height:
        raise ComparisonError("the entity-group query maps an entity to two groups")
    return frame


# -------------------------------------------------------------------------- the pairing


def _mean(values: pl.Series) -> float:
    """The mean of a non-empty numeric or boolean series, as a float."""
    return float(cast(float, values.mean()))


def paired_rows(
    pool: DictRowPool, experiment_hash: str, model_group_id: int, baseline_group_id: int
) -> pl.DataFrame:
    """The labeled rows both groups scored, one row per ``(entity, date)``.

    Columns ``entity_id, as_of_date, outcome, score_m, score_b, err_m, err_b, band,
    band_ix, sbc_class, cold_start``: ``err_*`` is the absolute error, and the segments are
    the model group's.
    """
    model = _scored(pool, experiment_hash, model_group_id)
    base = _scored(pool, experiment_hash, baseline_group_id)
    paired = model.join(
        base.select("entity_id", "as_of_date", pl.col("score").alias("score_b")),
        on=["entity_id", "as_of_date"],
        how="inner",
    )
    return paired.rename({"score": "score_m"}).with_columns(
        (pl.col("score_m") - pl.col("outcome")).abs().alias("err_m"),
        (pl.col("score_b") - pl.col("outcome")).abs().alias("err_b"),
    )


def _segment_row(
    cut: str, segment: str, order: int, part: pl.DataFrame
) -> dict[str, Any]:
    sum_y = float(part["outcome"].abs().sum())
    out: dict[str, Any] = {
        "cut": cut,
        "segment": segment,
        "segment_order": order,
        "n": part.height,
        "entities": part["entity_id"].n_unique(),
        "sum_outcome": float(part["outcome"].sum()),
    }
    for side in ("m", "b"):
        err = part[f"err_{side}"]
        out[f"mae_{side}"] = _mean(err)
        out[f"wape_{side}"] = float(err.sum()) / sum_y if sum_y else None
        out[f"bias_{side}"] = _mean(part[f"score_{side}"] - part["outcome"])
    out["model_wins"] = out["mae_m"] < out["mae_b"]
    out["closer_share"] = _mean(part["err_m"] < part["err_b"])
    out["excess_abs_error"] = float(part["err_m"].sum() - part["err_b"].sum())
    return out


def segment_table(
    paired: pl.DataFrame, subsets: Sequence[tuple[str, str, pl.DataFrame]] = ()
) -> list[dict[str, Any]]:
    """Per segment of every cut: both groups' errors, the win flag and the excess error.

    ``excess_share`` is the segment's share of the excess error summed over the segments of
    its cut where the model loses; it is ``None`` where the model wins.
    """
    rows: list[dict[str, Any]] = []
    for cut, column, order_col in (
        ("band", "band", "band_ix"),
        ("sbc", "sbc_class", None),
        ("cold_start", "cold_start", None),
    ):
        keyed = paired.with_columns(pl.col(column).cast(pl.Utf8).alias("_seg"))
        if cut == "cold_start":
            keyed = keyed.with_columns(
                pl.when(pl.col("cold_start"))
                .then(pl.lit("cold start"))
                .otherwise(pl.lit("with history"))
                .alias("_seg")
            )
        for (segment,), part in keyed.group_by("_seg", maintain_order=True):
            if order_col is not None:
                order = int(cast(int, part[order_col].fill_null(-1).min()))
            elif cut == "sbc":
                order = _SBC_ORDER.index(str(segment)) if segment in _SBC_ORDER else 99
            else:
                order = int(segment == "cold start")
            rows.append(_segment_row(cut, str(segment), order, part))
    for subset_hash, name, members in subsets:
        flagged = paired.join(members, on=["entity_id", "as_of_date"], how="left")
        inside = flagged.filter(pl.col("member").is_not_null())
        outside = flagged.filter(pl.col("member").is_null())
        cut = f"subset:{name}"
        for order, (segment, part) in enumerate(
            (("in subset", inside), ("outside subset", outside))
        ):
            if part.height:
                row = _segment_row(cut, segment, order, part)
                row["subset_hash"] = subset_hash
                rows.append(row)
    cut_order = {"band": 0, "sbc": 1, "cold_start": 2}
    rows.sort(
        key=lambda r: (
            cut_order.get(r["cut"], 3),
            r["cut"],
            r["segment_order"],
            r["segment"],
        )
    )
    losses: dict[str, float] = {}
    for r in rows:
        if r["excess_abs_error"] > 0:
            losses[r["cut"]] = losses.get(r["cut"], 0.0) + r["excess_abs_error"]
    for r in rows:
        total = losses.get(r["cut"], 0.0)
        r["excess_share"] = (
            r["excess_abs_error"] / total
            if r["excess_abs_error"] > 0 and total
            else None
        )
    return rows


_SBC_ORDER = (
    "smooth",
    "erratic",
    "intermittent",
    "lumpy",
    "one demand period",
    "no demand",
    "no history",
)


def entity_table(paired: pl.DataFrame, key: str = "entity_id") -> dict[str, Any]:
    """Each ``key``'s absolute error pooled over the dates, model against baseline."""
    pooled = paired.group_by(key).agg(
        pl.col("err_m").sum(), pl.col("err_b").sum(), pl.col("outcome").sum()
    )
    won = pooled.filter(pl.col("err_m") < pl.col("err_b"))
    tied = pooled.filter(pl.col("err_m") == pl.col("err_b"))
    total_y = float(pooled["outcome"].sum())
    ratios = pooled.filter(pl.col("err_b") > 0).select(
        (pl.col("err_m") / pl.col("err_b")).alias("r")
    )["r"]
    n = pooled.height
    return {
        "level": key,
        "n": n,
        "won_share": won.height / n if n else None,
        "tie_share": tied.height / n if n else None,
        "outcome_share_won": float(won["outcome"].sum()) / total_y if total_y else None,
        "median_error_ratio": float(cast(float, ratios.median()))
        if ratios.len()
        else None,
    }


def poisson_binomial_pmf(p: Sequence[float]) -> np.ndarray:
    """P(k successes) for independent trials with success probabilities ``p``."""
    pmf = np.array([1.0])
    for q in p:
        pmf = np.convolve(pmf, [1.0 - q, q])
    return pmf


def merge_cells(
    observed: np.ndarray, expected: np.ndarray, floor: float = MIN_EXPECTED
):
    """Adjacent cells merged from the sparser tail until each expects ``floor`` or more.

    Returns ``(observed, expected, edges)`` where ``edges[i]`` is the ``(lo, hi)`` range of
    win counts in merged cell ``i``.
    """
    cells = [
        [float(o), float(e), k, k]
        for k, (o, e) in enumerate(zip(observed, expected, strict=True))
    ]
    while len(cells) > 1:
        sparse = [i for i, c in enumerate(cells) if c[1] < floor]
        if not sparse:
            break
        i = min(sparse, key=lambda j: (cells[j][1], j))
        j = (
            i + 1
            if i == 0
            else i - 1
            if i == len(cells) - 1
            else (i - 1 if cells[i - 1][1] <= cells[i + 1][1] else i + 1)
        )
        lo, hi = sorted((i, j))
        a, b = cells[lo], cells[hi]
        cells[lo : hi + 1] = [[a[0] + b[0], a[1] + b[1], a[2], b[3]]]
    return (
        np.array([c[0] for c in cells]),
        np.array([c[1] for c in cells]),
        [(int(c[2]), int(c[3])) for c in cells],
    )


def chance_test(
    paired: pl.DataFrame, dates: Sequence[date]
) -> Optional[dict[str, Any]]:
    """Are the model's wins concentrated on the same entities more than chance allows?

    On the entities scored on every date in ``dates``: each date's win share ``p_d``, each
    entity's count of dates won, and the Poisson-binomial count expected under independence.
    ``None`` with fewer than two dates or no entity present on all of them.
    """
    from scipy.stats import chisquare

    if len(dates) < 2:
        return None
    complete = (
        paired.group_by("entity_id")
        .agg(pl.col("as_of_date").n_unique().alias("n"))
        .filter(pl.col("n") == len(dates))
        .select("entity_id")
    )
    rows = paired.join(complete, on="entity_id").with_columns(
        (pl.col("err_m") < pl.col("err_b")).alias("won")
    )
    n_entities = complete.height
    if not n_entities:
        return None
    by_date = rows.group_by("as_of_date").agg(pl.col("won").mean()).sort("as_of_date")
    p = [float(v) for v in by_date["won"]]
    counts = rows.group_by("entity_id").agg(pl.col("won").sum().alias("k"))
    observed = np.bincount(counts["k"].to_numpy(), minlength=len(dates) + 1).astype(
        float
    )
    expected = n_entities * poisson_binomial_pmf(p)
    obs_m, exp_m, edges = merge_cells(observed, expected)
    if len(obs_m) > 1:
        test = chisquare(obs_m, exp_m)
        statistic, p_value = float(test.statistic), float(test.pvalue)
    else:
        statistic, p_value = None, None
    last = max(dates)
    wide = rows.with_columns(pl.col("as_of_date").cast(pl.Utf8)).pivot(
        on="as_of_date", index="entity_id", values="won"
    )
    earlier = [str(d) for d in sorted(dates) if d != last]
    won_all = wide.filter(pl.all_horizontal(*[pl.col(c) for c in earlier]))
    return {
        "n_entities": n_entities,
        "n_dates": len(dates),
        "win_share_by_date": dict(
            zip([str(d) for d in by_date["as_of_date"]], p, strict=True)
        ),
        "observed": observed.tolist(),
        "expected": expected.tolist(),
        "cells": [
            {
                "wins": f"{lo}" if lo == hi else f"{lo}–{hi}",
                "observed": o,
                "expected": e,
            }
            for (lo, hi), o, e in zip(
                edges, obs_m.tolist(), exp_m.tolist(), strict=True
            )
        ],
        "chi_square": statistic,
        "dof": len(obs_m) - 1,
        "p_value": p_value,
        "p_win_last": p[-1],
        "n_won_all_earlier": won_all.height,
        "p_win_last_given_all_earlier": (
            _mean(won_all[str(last)]) if won_all.height else None
        ),
    }


def out_of_time(
    paired: pl.DataFrame, dates: Sequence[date]
) -> Optional[dict[str, Any]]:
    """The entities the model won over the earlier dates, scored on the last one."""
    if len(dates) < 2:
        return None
    last = max(dates)
    earlier = paired.filter(pl.col("as_of_date") < last)
    leaders = (
        earlier.group_by("entity_id")
        .agg(pl.col("err_m").sum(), pl.col("err_b").sum())
        .filter(pl.col("err_m") < pl.col("err_b"))
        .select("entity_id")
    )
    scored = paired.filter(pl.col("as_of_date") == last).join(leaders, on="entity_id")
    return {
        "last_date": last,
        "n_leaders": leaders.height,
        "n_rows": scored.height,
        "mae_m": _mean(scored["err_m"]) if scored.height else None,
        "mae_b": _mean(scored["err_b"]) if scored.height else None,
    }


# ------------------------------------------------------------------------------ entry


def compare_to_baseline(
    pool: DictRowPool,
    model_group_id: int,
    baseline_group_id: Optional[int] = None,
    *,
    experiment_hash: Optional[str] = None,
    cut: str = "all",
    entity_groups: Optional[pl.DataFrame] = None,
) -> Comparison:
    """Compare a model group with a baseline group on the rows both scored.

    Args:
        pool: the project database.
        model_group_id: the model group.
        baseline_group_id: the baseline; ``None`` picks :func:`default_baseline`.
        experiment_hash: the Experiment (a prefix is enough); ``None`` when the group's
            models all belong to one.
        cut: ``band``, ``sbc``, ``cold-start``, ``subset:NAME`` or ``all``.
        entity_groups: ``entity_id, group_key`` rows; repeats the entity table per group.

    Raises:
        ComparisonError: unknown groups, a non-regression Experiment, no shared row.
    """
    experiment = group_experiment(pool, model_group_id, experiment_hash)
    _check_regression(pool, experiment)
    chosen = baseline_group_id is None
    baseline = (
        default_baseline(pool, experiment, model_group_id)
        if baseline_group_id is None
        else baseline_group_id
    )
    if baseline == model_group_id:
        raise ComparisonError("the baseline must be another model group")
    group_experiment(pool, baseline, experiment)  # the baseline is in the Experiment
    paired = paired_rows(pool, experiment, model_group_id, baseline)
    if not paired.height:
        raise ComparisonError(
            f"groups {model_group_id} and {baseline} share no labeled scored row in"
            f" experiment {experiment[:12]}"
        )
    dates = sorted(set(paired["as_of_date"].to_list()))
    wanted = "cold_start" if cut == "cold-start" else cut
    subsets: list[tuple[str, str, pl.DataFrame]] = []
    if wanted == "all" or wanted.startswith("subset:"):
        for subset_hash, name in _subsets(pool, experiment):
            if wanted == "all" or wanted == f"subset:{name}":
                subsets.append((subset_hash, name, _members(pool, subset_hash)))
        if wanted.startswith("subset:") and not subsets:
            raise ComparisonError(
                f"experiment {experiment[:12]} was not evaluated on a Subset named"
                f" {wanted[len('subset:') :]!r}"
            )
    elif wanted not in HISTORY_CUTS:
        raise ComparisonError(
            f"{cut!r} is not a cut — expected band | sbc | cold-start | subset:NAME | all"
        )
    segments = segment_table(paired, subsets)
    if wanted != "all":
        segments = [r for r in segments if r["cut"] == wanted]
    won = [r["model_wins"] for r in segments]
    comparison = Comparison(
        experiment_hash=experiment,
        model_group_id=model_group_id,
        baseline_group_id=baseline,
        baseline_chosen=chosen,
        dates=dates,
        n_rows=paired.height,
        segments=segments,
        segment_win_share=sum(won) / len(won) if won else None,
        entities=entity_table(paired),
        chance=chance_test(paired, dates),
        out_of_time=out_of_time(paired, dates),
    )
    if entity_groups is not None:
        keyed = paired.join(entity_groups, on="entity_id", how="inner")
        comparison.entity_groups = entity_table(keyed, "group_key")
        comparison.entity_groups["n_unmapped_rows"] = paired.height - keyed.height
    return comparison
