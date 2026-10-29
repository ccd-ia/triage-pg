"""Null-data tests for triage-pg's own spine: shuffled sources, controls, flips.

The randomization test of Walsh & Wang (``dssg/randomize_your_data``; Ghani, Walsh
& Wang, "Top 10 ways your Machine Learning models may have leakage", DSSG 2020):
shuffle the raw data so that no feature can carry information about the label, run
the whole pipeline, and the test metrics must sit at chance. Here it guards
triage-pg's own matrix assembly → fit → score → in-PG evaluation spine against a
regression that lets test information reach training. It is a suite test, not a
user command.

* **The null.** Five shuffles of a synthetic Source pair, each run end-to-end
  through :func:`~triage.adapters.run.run_experiment`. The mean test AUC must lie
  within 0.5 ± 4σ/√5, where σ² = (n₁ + n₀ + 1)/(12·n₁·n₀) is the null variance of
  the Mann–Whitney AUC, and each precision@k within base rate ± 4·√(p(1−p)/k).
* **The controls.** On unshuffled data the same experiment must find the planted
  signal (AUC ≥ 0.75). On shuffled data two planted leaks must break the null; a null
  that a real leak cannot break proves nothing. A train-on-test leak must reach
  AUC ≥ 0.9, and features that read the label window AUC ≥ 0.75. The second holds
  only because past outcomes are features (see ``_experiment_config``).
* **Flip invariants.** Flipping every label must mirror the stored metrics:
  AUC → 1 − AUC, and precision@k → 1 − precision@k when every row in the top k is
  labeled (ADR-0033).

Two properties of the generator and the shuffle keep the null exact:

* Every entity has exactly one event per month (a uniform event rate). An
  independent per-column shuffle keeps each entity id's frequency, and with an
  "any event in the window" label that frequency alone predicts the label without
  any leakage.
* Every non-key column is permuted *within its date*. Dates stay in place, so every
  window keeps its data, and every entity keeps one event per month. A permutation
  of ``entity_id`` across all dates keeps each entity's total but scatters it over
  the windows: an entity with more events in its feature window then has fewer left
  for its label window, which predicts an "any event" label for the same reason,
  one level down.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
from psycopg import sql

from triage.adapters import model as model_adapter
from triage.adapters.matrix import MatrixResult
from triage.adapters.run import ExperimentResult, run_experiment
from triage.component.catwalk.in_pg_evaluation import evaluate_in_db
from triage.profiles.storage import LocalStorage
from triage.util.db import DictRowPool, returned_row

# ---------------------------------------------------------------------------
# The synthetic Sources
# ---------------------------------------------------------------------------

GENERATOR_SEED = 2026  # the data; the shuffle seeds below are the null's draws
MODEL_SEED = 42
NULL_SEEDS = (1, 2, 3, 4, 5)

N_ENTITIES = 600
N_MONTHS = 27  # one event per entity on the 15th of every month, 2014-01 … 2016-03
START_DATE = date(2013, 1, 1)  # every entity exists before the first as_of_date
# outcome ~ Bernoulli(sigmoid(SLOPE·risk + INTERCEPT)) per event; value = risk + noise.
# Unshuffled, the 3-month "any event" label has a base rate near one third, and a small
# RandomForest on a year of values and outcomes reaches a test AUC near 0.88.
SIGNAL_SLOPE = 2.0
SIGNAL_INTERCEPT = -2.5
VALUE_NOISE = 1.0
# Events whose value was not recorded. A one-month aggregate over a missing value is
# NULL, so the matrices carry a fit-based (mean) imputation whose statistic differs
# between the train and test dates: the ADR-0009 revert-check needs a statistic that
# refitting on the test rows can move.
VALUE_MISSING_RATE = 0.3

# One timechop split: train on 2015-01-01 … 2015-10-01, test on 2016-01-01.
TEST_DATE = date(2016, 1, 1)
LABEL_TIMESPAN = "3 months"
TEMPORAL_CONFIG = {
    "feature_start_time": "2014-01-01",
    "feature_end_time": "2016-04-01",
    "label_start_time": "2015-01-01",
    "label_end_time": "2016-04-01",
    "model_update_frequency": "1 year",
    "training_as_of_date_frequencies": "3 months",
    "test_as_of_date_frequencies": "3 months",
    "max_training_histories": "9 months",
    "test_durations": "0 days",
    "label_timespans": LABEL_TIMESPAN,
}

COHORT_QUERY = "select entity_id from entities where start_date <= date {as_of_date}"
# Any positive event in [as_of_date, as_of_date + label_timespan): three per entity.
LABEL_QUERY = (
    "select entity_id, max(outcome) as outcome from events"
    " where event_date >= date {as_of_date}"
    " and event_date < date {as_of_date} + {label_timespan}"
    " group by entity_id"
)

TOP_K = 100
PRECISION_PARAMETER = f"{TOP_K}_abs"  # in the default classification threshold set
FOREST_GRID = {
    "sklearn.ensemble.RandomForestClassifier": {
        "n_estimators": [50],
        "max_depth": [5],
        "min_samples_leaf": [5],
    }
}
DEEP_TREE_GRID = {"sklearn.tree.DecisionTreeClassifier": {"max_depth": [None]}}

# Each Source: (relation, key column, the date column the shuffle is stratified by).
SOURCES = (
    ("entities", "entity_id", "start_date"),
    ("events", "event_id", "event_date"),
)


@dataclass(frozen=True)
class SyntheticSources:
    """Column-wise rows of the two Sources, ready for ``unnest``."""

    entities: dict[str, list[Any]]
    events: dict[str, list[Any]]


def _event_date(month: int) -> date:
    """The 15th of the ``month``-th month after 2014-01."""
    return date(2014 + month // 12, 1 + month % 12, 15)


def generate_sources(seed: int = GENERATOR_SEED) -> SyntheticSources:
    """Generate the entity and event Sources with a planted, learnable signal.

    Each entity has a latent ``risk`` and a static attribute ``x_static`` (``risk``
    plus noise). Every month it has one event whose ``value`` is ``risk`` plus noise
    (missing at random for ``VALUE_MISSING_RATE`` of the events) and whose
    ``outcome`` is positive with probability
    ``sigmoid(SIGNAL_SLOPE·risk + SIGNAL_INTERCEPT)``. The features (``x_static`` and
    aggregates of past values and past outcomes) therefore predict the label (any
    positive outcome in the next three months). The event rate is the same for every
    entity; the module docstring says why the null needs that.

    Args:
        seed: the generator's seed.

    Returns:
        The rows of both Sources, column by column.
    """
    rng = np.random.default_rng(seed)
    risk = rng.normal(size=N_ENTITIES)
    entity_ids = list(range(1, N_ENTITIES + 1))
    x_static = risk + rng.normal(size=N_ENTITIES)

    n_events = N_ENTITIES * N_MONTHS
    value = np.repeat(risk, N_MONTHS) + rng.normal(scale=VALUE_NOISE, size=n_events)
    missing = rng.random(n_events) < VALUE_MISSING_RATE
    p_positive = 1.0 / (1.0 + np.exp(-(SIGNAL_SLOPE * risk + SIGNAL_INTERCEPT)))
    outcome = rng.random((N_ENTITIES, N_MONTHS)) < p_positive[:, None]
    dates = [_event_date(month) for month in range(N_MONTHS)]

    return SyntheticSources(
        entities={
            "entity_id": entity_ids,
            "start_date": [START_DATE] * N_ENTITIES,
            "x_static": [float(x) for x in x_static],
        },
        events={
            "event_id": list(range(1, n_events + 1)),
            "entity_id": [e for e in entity_ids for _ in range(N_MONTHS)],
            "event_date": dates * N_ENTITIES,
            "value": [
                None if gone else float(v)
                for v, gone in zip(value, missing, strict=True)
            ],
            "outcome": [float(o) for o in outcome.ravel()],
        },
    )


def seed_sources(engine: DictRowPool, sources: SyntheticSources) -> None:
    """(Re)create both Source tables and load ``sources`` into them."""
    with engine.connection() as conn:
        conn.execute("drop table if exists events, entities")
        conn.execute(
            "create table entities"
            " (entity_id bigint primary key, start_date date not null,"
            "  x_static double precision)"
        )
        conn.execute(
            "insert into entities (entity_id, start_date, x_static)"
            " select * from unnest(%(entity_id)s::bigint[], %(start_date)s::date[],"
            "  %(x_static)s::double precision[])",
            sources.entities,
        )
        conn.execute(
            "create table events"
            " (event_id bigint primary key, entity_id bigint not null,"
            "  event_date date not null, value double precision,"
            "  outcome double precision not null)"
        )
        conn.execute(
            "insert into events (event_id, entity_id, event_date, value, outcome)"
            " select * from unnest(%(event_id)s::bigint[], %(entity_id)s::bigint[],"
            "  %(event_date)s::date[], %(value)s::double precision[],"
            "  %(outcome)s::double precision[])",
            sources.events,
        )


def shuffle_sources(engine: DictRowPool, seed: int) -> None:
    """Independently permute every non-key column of every Source, within each date.

    Keys and dates stay in place. Every other column (``x_static``; the events'
    ``entity_id``, ``value`` and ``outcome``) gets its own permutation inside each date
    stratum, read from the catalog so no column is missed. Afterwards no column carries
    information about any other, and every entity still has one event per month.

    Args:
        engine: the project database pool.
        seed: the shuffle's seed; the same seed gives the same shuffle.
    """
    rng = np.random.default_rng(seed)
    with engine.connection() as conn:
        for relation, key, stratum in SOURCES:
            columns = [
                (row["attname"], row["type"])
                for row in conn.execute(
                    "select attname, format_type(atttypid, atttypmod) as type"
                    " from pg_attribute"
                    " where attrelid = cast(%(rel)s as regclass) and attnum > 0"
                    "  and not attisdropped and attname not in (%(key)s, %(stratum)s)"
                    " order by attnum",
                    {"rel": relation, "key": key, "stratum": stratum},
                ).fetchall()
            ]
            rows = conn.execute(
                sql.SQL(
                    "select {key}, {stratum}, {cols} from {rel} order by {key}"
                ).format(
                    key=sql.Identifier(key),
                    stratum=sql.Identifier(stratum),
                    cols=sql.SQL(", ").join(sql.Identifier(c) for c, _ in columns),
                    rel=sql.Identifier(relation),
                )
            ).fetchall()
            strata: dict[Any, list[int]] = {}
            for position, row in enumerate(rows):
                strata.setdefault(row[stratum], []).append(position)
            groups = [np.asarray(strata[k]) for k in sorted(strata)]

            shuffled: dict[str, list[Any]] = {"key": [row[key] for row in rows]}
            for column, _ in columns:
                values = np.empty(len(rows), dtype=object)
                values[:] = [row[column] for row in rows]
                permuted = values.copy()
                for group in groups:
                    permuted[group] = values[group[rng.permutation(len(group))]]
                shuffled[column] = permuted.tolist()

            conn.execute(
                sql.SQL(
                    "update {rel} as t set {sets}"
                    " from (select * from unnest({arrays})) as s ({names})"
                    " where t.{key} = s.key"
                ).format(
                    rel=sql.Identifier(relation),
                    sets=sql.SQL(", ").join(
                        sql.SQL("{c} = s.{c}").format(c=sql.Identifier(c))
                        for c, _ in columns
                    ),
                    arrays=sql.SQL(", ").join(
                        [sql.SQL("cast(%(key)s as bigint[])")]
                        + [
                            sql.SQL("cast({p} as {t}[])").format(
                                p=sql.Placeholder(c), t=sql.SQL(t)
                            )
                            for c, t in columns
                        ]
                    ),
                    names=sql.SQL(", ").join(
                        [sql.Identifier("key")]
                        + [sql.Identifier(c) for c, _ in columns]
                    ),
                    key=sql.Identifier(key),
                ),
                shuffled,
            )


# ---------------------------------------------------------------------------
# The experiment and its stored test metrics
# ---------------------------------------------------------------------------


def _experiment_config(
    grid_config: dict[str, Any], version_label: str, events_table: str = "events"
) -> dict[str, Any]:
    """The experiment over the synthetic Sources.

    ``version_label`` pins the Sources (ADR-0014): each shuffle gets its own label, so a
    second run in the same database rebuilds every artifact instead of reusing the first
    run's cohort, labels, matrices and models. ``events_table`` is the relation the
    features read the events from; the cohort and the labels always read ``events``.
    """
    return {
        "problem_type": "classification",
        "temporal_config": TEMPORAL_CONFIG,
        "cohort_config": {"query": COHORT_QUERY},
        "label_config": {"query": LABEL_QUERY},
        "feature_config": {
            "target": "entities",
            "max_depth": 2,
            # P31D holds exactly one event (the previous month's) at every as_of_date.
            "intervals": ["P31D", "P365D"],
            "aggregations": ["mean", "max", "min", "count"],
            "transformations": ["identity"],
            "entities": [
                {
                    "alias": "entities",
                    "id": "entity_id",
                    "table": "entities",
                    "temporal_ix": "start_date",
                    "variables": {"x_static": {"type": "numeric"}},
                },
                {
                    "alias": "events",
                    "id": "event_id",
                    "table": events_table,
                    "temporal_ix": "event_date",
                    # Past outcomes are features, as prior failed inspections are in a
                    # real early-warning model. Without them the null could not see a
                    # time leak: the shuffle makes ``value`` independent of ``outcome``,
                    # so features that read the label window would still carry nothing.
                    "variables": {
                        "value": {"type": "numeric"},
                        "outcome": {"type": "numeric"},
                    },
                },
            ],
            "relationships": [
                {
                    "parent": {"entity": "entities", "key": "entity_id"},
                    "child": {"entity": "events", "key": "entity_id"},
                    "temporal": {"mode": "as_of", "child_time": "event_date"},
                }
            ],
        },
        "imputation_config": {"all": {"type": "mean"}},
        "grid_config": grid_config,
        "sources": [
            {
                "name": "entities",
                "relation": "entities",
                "version_label": version_label,
            },
            {
                "name": "events",
                "relation": "events",
                "knowledge_date_column": "event_date",
                "version_label": version_label,
            },
        ],
    }


def _run(
    engine: DictRowPool,
    storage_root: Path,
    grid_config: dict[str, Any],
    version_label: str,
    events_table: str = "events",
) -> ExperimentResult:
    """Run the experiment end-to-end on whatever the Source tables hold now."""
    return run_experiment(
        engine,
        _experiment_config(grid_config, version_label, events_table),
        storage=LocalStorage(),
        storage_root=str(storage_root),
        random_seed=MODEL_SEED,
    )


def _test_metrics(
    engine: DictRowPool, experiment: ExperimentResult
) -> dict[tuple[str, str], dict[str, Any]]:
    """The experiment's one model's full-cohort test evaluations, by (metric, param)."""
    (run,) = experiment.runs
    (model_id,) = run.model_ids
    with engine.connection() as conn:
        rows = conn.execute(
            "select as_of_date, metric, parameter, value, num_labeled, num_positive,"
            "       num_labeled_above_threshold"
            " from triage.evaluations"
            " where model_id = %(m)s and split_kind = 'test' and subset_hash = ''",
            {"m": model_id},
        ).fetchall()
    assert {row["as_of_date"] for row in rows} == {TEST_DATE}
    return {(row["metric"], row["parameter"]): row for row in rows}


def _auc_null_variance(n_positive: int, n_negative: int) -> float:
    """The null variance of the Mann–Whitney AUC without ties (ties shrink it)."""
    return (n_positive + n_negative + 1) / (12.0 * n_positive * n_negative)


def _train_on_test(engine: DictRowPool, scratch: Path):
    """A ``_fit_estimator`` that also trains on the split's test matrix: a planted leak.

    The test matrix is found through the artifact DAG: it is the one test matrix whose
    parents include the train matrix (the ADR-0009 leakage-boundary edge). Its rows
    are appended to the train rows in a scratch Parquet file, and the real fit runs on
    that file.
    """
    fit = model_adapter._fit_estimator
    scratch.mkdir(parents=True, exist_ok=True)

    def leaky_fit(
        train_matrix_result: MatrixResult,
        class_path: str,
        hyperparameters: dict[str, Any],
        random_seed: int,
        storage: Any = None,
    ):
        with engine.connection() as conn:
            test_uris = [
                row["storage_uri"]
                for row in conn.execute(
                    "select m.storage_uri"
                    " from triage.artifact_inputs as ai"
                    " inner join triage.matrices as m"
                    "  on m.artifact_id = ai.artifact_id"
                    " where ai.parent_id = %(train)s and m.matrix_kind = 'test'",
                    {"train": train_matrix_result.matrix_artifact_id},
                ).fetchall()
            ]
        assert len(test_uris) == 1, (
            f"expected one test matrix built from train matrix"
            f" {train_matrix_result.matrix_artifact_id}, found {test_uris}"
        )
        columns = [*train_matrix_result.feature_names, "outcome"]
        leaked = pl.concat(
            [
                pl.read_parquet(uri).select(columns)
                for uri in (train_matrix_result.storage_uri, test_uris[0])
            ]
        )
        leaked_uri = scratch / f"{train_matrix_result.matrix_artifact_id}.parquet"
        leaked.write_parquet(leaked_uri)
        return fit(
            replace(train_matrix_result, storage_uri=str(leaked_uri)),
            class_path,
            hyperparameters,
            random_seed,
            storage,
        )

    return leaky_fit


# ---------------------------------------------------------------------------
# The null and its two controls
# ---------------------------------------------------------------------------


def test_shuffle_keeps_dates_marginals_and_the_uniform_rate(db_pool_greenfield):
    """The properties the null rests on survive the shuffle.

    Keys, dates, each date's values and one event per entity per month stay; almost
    every event changes entity.
    """
    engine = db_pool_greenfield
    seed_sources(engine, generate_sources())
    per_date = (
        "select event_date, array_agg(value order by value) as v,"
        "       array_agg(outcome order by outcome) as o"
        " from events group by event_date order by event_date"
    )
    with engine.connection() as conn:
        before = conn.execute(per_date).fetchall()
        original = conn.execute(
            "select event_id, entity_id from events order by event_id"
        ).fetchall()

    shuffle_sources(engine, NULL_SEEDS[0])

    with engine.connection() as conn:
        assert conn.execute(per_date).fetchall() == before
        not_uniform = conn.execute(
            "select entity_id, date_trunc('month', event_date) as month, count(*) as n"
            " from events group by 1, 2 having count(*) <> 1"
        ).fetchall()
        assert not_uniform == []
        shuffled = conn.execute(
            "select event_id, entity_id from events order by event_id"
        ).fetchall()
    assert [r["event_id"] for r in shuffled] == [r["event_id"] for r in original]
    moved = sum(
        a["entity_id"] != b["entity_id"]
        for a, b in zip(original, shuffled, strict=True)
    )
    assert moved > 0.99 * len(original)


def test_null_shuffled_sources_score_at_chance(db_pool_greenfield, tmp_path):
    """Five shuffles: mean AUC within 0.5 ± 4σ/√5, precision@k within its base rate."""
    engine = db_pool_greenfield
    sources = generate_sources()

    aucs: list[float] = []
    variances: list[float] = []
    for seed in NULL_SEEDS:
        seed_sources(engine, sources)
        shuffle_sources(engine, seed)
        metrics = _test_metrics(
            engine,
            _run(engine, tmp_path / "store", FOREST_GRID, f"shuffle-{seed}"),
        )

        auc = metrics[("auc_roc", "")]
        n_positive = auc["num_positive"]
        n_negative = auc["num_labeled"] - n_positive
        assert auc["num_labeled"] == N_ENTITIES  # every cohort member is labeled
        aucs.append(auc["value"])
        variances.append(_auc_null_variance(n_positive, n_negative))

        precision = metrics[("precision@", PRECISION_PARAMETER)]
        assert precision["num_labeled_above_threshold"] == TOP_K
        base_rate = precision["num_positive"] / precision["num_labeled"]
        band = 4 * math.sqrt(base_rate * (1 - base_rate) / TOP_K)
        assert abs(precision["value"] - base_rate) <= band, (
            f"shuffle seed {seed}: precision@{PRECISION_PARAMETER} ="
            f" {precision['value']:.4f} is outside base rate {base_rate:.4f} ±"
            f" {band:.4f}: the spine ranks shuffled data above chance; look for test"
            " rows reaching training"
        )

    mean_auc = sum(aucs) / len(aucs)
    # The mean of n independent AUCs has variance Σσᵢ²/n², σ²/n when the σᵢ are equal.
    band = 4 * math.sqrt(sum(variances)) / len(variances)
    assert abs(mean_auc - 0.5) <= band, (
        f"mean test AUC over shuffle seeds {NULL_SEEDS} = {mean_auc:.4f} (per seed"
        f" {[round(a, 4) for a in aucs]}) is outside 0.5 ± {band:.4f}: the spine"
        " finds signal in shuffled data; look for test rows reaching training"
    )


def test_signal_control_unshuffled_sources_rank_above_chance(
    db_pool_greenfield, tmp_path
):
    """The planted signal is learnable: a null at 0.5 is not a model that learns
    nothing."""
    engine = db_pool_greenfield
    seed_sources(engine, generate_sources())

    metrics = _test_metrics(
        engine, _run(engine, tmp_path / "store", FOREST_GRID, "unshuffled")
    )

    auc = metrics[("auc_roc", "")]["value"]
    assert auc >= 0.75, (
        f"unshuffled test AUC = {auc:.4f} < 0.75: the generator's signal is too weak"
        " for the null test to mean anything; check SIGNAL_SLOPE and VALUE_NOISE"
    )


def test_power_control_train_on_test_leak_breaks_the_null(
    db_pool_greenfield, tmp_path, monkeypatch
):
    """A deep tree that also trains on the test rows scores shuffled data far above
    chance.

    This is the null test's power: the leak it exists to catch moves the AUC from
    0.5 ± 0.05 to at least 0.9.
    """
    engine = db_pool_greenfield
    seed_sources(engine, generate_sources())
    shuffle_sources(engine, NULL_SEEDS[0])
    monkeypatch.setattr(
        model_adapter, "_fit_estimator", _train_on_test(engine, tmp_path / "leaked")
    )

    metrics = _test_metrics(
        engine, _run(engine, tmp_path / "store", DEEP_TREE_GRID, "train-on-test")
    )

    auc = metrics[("auc_roc", "")]["value"]
    assert auc >= 0.9, (
        f"a planted train-on-test leak scored test AUC = {auc:.4f} < 0.9 on shuffled"
        " data: the null test cannot tell this leak from chance"
    )


def test_power_control_features_reading_the_label_window_break_the_null(
    db_pool_greenfield, tmp_path
):
    """Features that read the label window score shuffled data far above chance.

    The features read the events through a view whose dates run one label timespan
    early, so a feature at an as_of_date aggregates the events of that date's own label
    window: what a matrix assembly that joins features from the wrong date, or a
    featurizer that loses its causal upper bound, would hand the model. The null test's
    own forest must find it. It can only because past outcomes are features: the
    shuffle makes ``value`` independent of ``outcome``, so a leak that carries only
    ``value`` carries nothing about the label.
    """
    engine = db_pool_greenfield
    seed_sources(engine, generate_sources())
    shuffle_sources(engine, NULL_SEEDS[0])
    with engine.connection() as conn:
        conn.execute(
            sql.SQL(
                "create view events_ahead as"
                " select event_id, entity_id, value, outcome,"
                "  cast(event_date - cast({span} as interval) as date) as event_date"
                " from events"
            ).format(span=sql.Literal(LABEL_TIMESPAN))
        )

    metrics = _test_metrics(
        engine,
        _run(engine, tmp_path / "store", FOREST_GRID, "label-window", "events_ahead"),
    )

    auc = metrics[("auc_roc", "")]["value"]
    assert auc >= 0.75, (
        f"features that read the label window scored test AUC = {auc:.4f} < 0.75 on"
        " shuffled data: the null test cannot tell a feature-side time leak from chance"
    )


# ---------------------------------------------------------------------------
# Flip invariants of the evaluation SQL
# ---------------------------------------------------------------------------

FLIP_AS_OF_DATE = date(2014, 1, 1)
FLIP_TIMESPAN = "6 months"
# Each threshold's k. 10_pct is ceil(10% of the 220 scored rows), labeled or not
# (ADR-0033).
FLIP_THRESHOLDS = {"25_abs": 25, "60_abs": 60, "10_pct": 22}
N_FLIP_LABELED = 200
N_FLIP_UNLABELED = 20


def _seed_flip_model(engine: DictRowPool) -> int:
    """One model with 220 test predictions: 200 labeled, 20 unlabeled scored lowest.

    Scores are rounded to two decimals, so the ranking has ties (the mid-rank AUC must
    still mirror). Labels follow the scores loosely, so the AUC is well away from 0.5
    and the flip moves it.
    """
    rng = np.random.default_rng(GENERATOR_SEED)
    labeled_scores = np.round(rng.uniform(0.1, 1.0, N_FLIP_LABELED), 2)
    outcomes = (rng.random(N_FLIP_LABELED) < labeled_scores * 0.6).astype(float)
    unlabeled_scores = np.round(rng.uniform(0.0, 0.05, N_FLIP_UNLABELED), 3)

    with engine.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values"
            " ('flip-model', 'flip-model', 'model', '{}'::jsonb),"
            " ('flip-labels', 'flip-labels', 'labels', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.model_groups"
            " (model_group_hash, model_type, hyperparameters, feature_list)"
            " values ('flip-group', 'sklearn.tree.DecisionTreeClassifier', '{}'::jsonb,"
            "  array['f1'])"
        )
        model_id = returned_row(
            conn.execute(
                "insert into triage.models (model_group_id, model_hash, train_end_time)"
                " select model_group_id, 'flip-model', date '2013-07-01'"
                " from triage.model_groups where model_group_hash = 'flip-group'"
                " returning model_id"
            ).fetchone()
        )["model_id"]
        scores = [float(s) for s in (*labeled_scores, *unlabeled_scores)]
        conn.execute(
            "insert into triage.predictions"
            " (model_id, entity_id, as_of_date, split_kind, score)"
            " select %(m)s, e, %(d)s, 'test', s"
            " from unnest(%(e)s::bigint[], %(s)s::double precision[]) as u (e, s)",
            {
                "m": model_id,
                "d": FLIP_AS_OF_DATE,
                "e": list(range(1, len(scores) + 1)),
                "s": scores,
            },
        )
        conn.execute(
            "insert into triage.labels"
            " (label_hash, entity_id, as_of_date, label_timespan, outcome)"
            " select 'flip-labels', e, %(d)s, cast(%(ts)s as interval), o"
            " from unnest(%(e)s::bigint[], %(o)s::double precision[]) as u (e, o)",
            {
                "d": FLIP_AS_OF_DATE,
                "ts": FLIP_TIMESPAN,
                "e": list(range(1, N_FLIP_LABELED + 1)),
                "o": [float(o) for o in outcomes],
            },
        )
    return model_id


def _evaluate_flip_model(
    engine: DictRowPool, model_id: int
) -> dict[tuple[str, str], dict[str, Any]]:
    evaluate_in_db(
        engine,
        model_id,
        FLIP_AS_OF_DATE,
        FLIP_TIMESPAN,
        metric_config={
            "metrics": ["precision@", "auc_roc"],
            "thresholds": list(FLIP_THRESHOLDS),
        },
    )
    with engine.connection() as conn:
        rows = conn.execute(
            "select metric, parameter, value, num_labeled_above_threshold"
            " from triage.evaluations"
            " where model_id = %(m)s and as_of_date = %(d)s and subset_hash = ''",
            {"m": model_id, "d": FLIP_AS_OF_DATE},
        ).fetchall()
    return {(row["metric"], row["parameter"]): row for row in rows}


def test_label_flip_mirrors_auc_and_precision(db_pool_greenfield):
    """Flipping every label (0 ↔ 1) maps AUC → 1 − AUC and precision@k → 1 − p@k."""
    engine = db_pool_greenfield
    model_id = _seed_flip_model(engine)

    before = _evaluate_flip_model(engine, model_id)
    with engine.connection() as conn:
        conn.execute(
            "update triage.labels set outcome = 1 - outcome"
            " where label_hash = 'flip-labels'"
        )
    after = _evaluate_flip_model(engine, model_id)

    auc = before[("auc_roc", "")]["value"]
    assert abs(auc - 0.5) > 0.1  # the fixture ranks, so the flip moves the AUC
    assert after[("auc_roc", "")]["value"] == pytest.approx(1 - auc, abs=1e-12)

    for threshold, k in FLIP_THRESHOLDS.items():
        key = ("precision@", threshold)
        # Every row in the cut is labeled: the unlabeled rows hold the lowest scores.
        assert before[key]["num_labeled_above_threshold"] == k
        assert after[key]["num_labeled_above_threshold"] == k
        assert after[key]["value"] == pytest.approx(1 - before[key]["value"], abs=1e-12)
