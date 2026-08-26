"""Pre-flight planning + data estimates (``triage analyze-config``).

Two halves, tested the way they are built: :func:`plan_experiment` needs no database at all
(featurizer's planner runs in ``Featurizer.__init__``), while :func:`estimate_data` is checked
against a real Postgres by *comparing it to what the real builders insert* — an estimate that
agrees with itself proves nothing.

The fan-out fixture reproduces the exact configuration that failed live on DirtyDuck: an
explicit ``definitions`` partition swept ``leave-one-out``, with a baseline pinned by name to a
column one of those runs drops.
"""

from pathlib import Path

import pytest
import yaml

from triage.adapters.cohort import build_cohort
from triage.adapters.labels import build_labels
from triage.adapters.preflight import (
    _count_projection,
    _pinned_feature_names,
    check_baseline_features,
    estimate_data,
    plan_experiment,
)

_DIRTYDUCK = (
    Path(__file__).resolve().parents[3] / "example" / "dirtyduck" / "experiment.yaml"
)

_RANKER = "triage.component.catwalk.baselines.rankers.BaselineRankMultiFeature"
_THRESHOLDER = "triage.component.catwalk.baselines.thresholders.SimpleThresholder"


@pytest.fixture(scope="module")
def dirtyduck_config():
    return yaml.safe_load(_DIRTYDUCK.read_text(encoding="utf-8"))


@pytest.fixture
def fanout_config(dirtyduck_config):
    """DirtyDuck plus the explicit two-group partition swept leave-one-out (3 runs)."""
    config = yaml.safe_load(yaml.safe_dump(dirtyduck_config))  # deep copy
    config["feature_config"]["feature_groups"] = {
        "definitions": {
            "facility_attrs": ["facilities.*"],
            "inspection_history": ["*(inspections.*"],
        },
        "strategies": ["all", "leave-one-out"],
    }
    return config


# ---------------------------------------------------------------------------
# DB-free planning
# ---------------------------------------------------------------------------


def test_plan_reproduces_the_canonical_dirtyduck_numbers(dirtyduck_config):
    """The published DirtyDuck run is 32 models; the plan must say so before it runs.

    Guards the whole point of the command: the two factors (grid 8, splits 4) were always
    printed, but nothing multiplied them, and 8 and 4 do not look like 32 to a reader.
    """
    plan = plan_experiment(dirtyduck_config)

    assert plan.grid_size == 8
    assert plan.n_splits == 4
    assert plan.n_runs == 1
    assert plan.n_models == 32
    assert plan.n_feature_columns == 147
    assert plan.feature_error is None


def test_plan_multiplies_the_feature_group_fan_out(fanout_config):
    """Three runs × 8 × 4 = 96 fits — the number a fan-out actually costs."""
    plan = plan_experiment(fanout_config)

    assert [s.label for s in plan.subsets] == [
        "all",
        "leave-one-out:facility_attrs",
        "leave-one-out:inspection_history",
    ]
    assert plan.n_runs == 3
    assert plan.n_models == 96
    # The partition itself: 27 + 120 = 147, the live-verified split.
    by_label = {s.label: s.n_columns for s in plan.subsets}
    assert by_label["all"] == 147
    assert by_label["leave-one-out:facility_attrs"] == 120
    assert by_label["leave-one-out:inspection_history"] == 27


def test_plan_survives_an_unplannable_feature_config(dirtyduck_config):
    """A broken ER graph must not hide a broken grid: splits + grid stay exact."""
    config = yaml.safe_load(yaml.safe_dump(dirtyduck_config))
    config["feature_config"] = {"entities": [{"alias": "nope"}], "target": "nope"}

    plan = plan_experiment(config)

    assert plan.feature_error is not None
    assert plan.n_feature_columns is None
    assert plan.grid_size == 8
    assert plan.n_splits == 4
    # With no manifest there is no fan-out to count, so the model count falls back to one run
    # rather than silently reporting zero.
    assert plan.n_models == 32


def test_plan_requires_temporal_config(dirtyduck_config):
    config = yaml.safe_load(yaml.safe_dump(dirtyduck_config))
    del config["temporal_config"]

    with pytest.raises(ValueError, match="temporal_config"):
        plan_experiment(config)


# ---------------------------------------------------------------------------
# The baseline pre-flight
# ---------------------------------------------------------------------------


def test_baseline_preflight_catches_the_column_a_leave_one_out_drops(fanout_config):
    """The live DirtyDuck failure, predicted before the matrix is built.

    ``BaselineRankMultiFeature`` and ``SimpleThresholder`` both pin
    ``COUNT(inspections.result)``; dropping the inspection group removes it and the run dies
    with ``BaselineFeatureNotInMatrix`` partway through training.
    """
    plan = plan_experiment(fanout_config)

    assert plan.baseline_issues, "the pinned-column conflict must be reported"
    flagged = {(i.class_path, i.subset_label) for i in plan.baseline_issues}
    assert (_RANKER, "leave-one-out:inspection_history") in flagged
    assert (_THRESHOLDER, "leave-one-out:inspection_history") in flagged
    # The runs that DO carry the column are not flagged — a false positive here would train
    # people to ignore the panel.
    assert not any(i.subset_label == "all" for i in plan.baseline_issues)
    assert all(
        i.missing == ("COUNT(inspections.result)",) for i in plan.baseline_issues
    )


def test_baseline_preflight_is_silent_when_every_run_keeps_the_column(dirtyduck_config):
    """No fan-out ⇒ one run holding all 147 columns ⇒ nothing to report."""
    plan = plan_experiment(dirtyduck_config)

    assert plan.baseline_issues == ()


def test_baseline_preflight_ignores_estimators_that_name_no_column(dirtyduck_config):
    """Only ``consumes_named_features`` estimators are checked — the rest take numpy."""
    issues = check_baseline_features(
        {"sklearn.tree.DecisionTreeClassifier": {"max_depth": [3]}},
        dirtyduck_config["feature_config"],
        ["COUNT(inspections.result)"],
    )

    assert issues == []


def test_baseline_preflight_reports_an_unconstructable_estimator(dirtyduck_config):
    """A malformed grid entry surfaces as an issue, never as a swallowed exception."""
    issues = check_baseline_features(
        {_RANKER: {"rules": [["not-a-dict"]]}},
        dirtyduck_config["feature_config"],
        ["COUNT(inspections.result)"],
    )

    assert len(issues) == 1
    assert issues[0].detail is not None
    assert "cannot be constructed" in issues[0].detail


class _AllFeatureNames:
    all_feature_names = ["a", "b"]


class _Features:
    features = ("x", "y")


class _SingleFeature:
    feature = "solo"


class _Plain:
    pass


@pytest.mark.parametrize(
    "estimator, expected",
    [
        (_AllFeatureNames(), ("a", "b")),
        (_Features(), ("x", "y")),
        (_SingleFeature(), ("solo",)),
        (_Plain(), ()),
    ],
)
def test_pinned_feature_names_covers_every_baseline_shape(estimator, expected):
    """The four shapes in catwalk: rules-based, weighted, single-column, and none."""
    assert _pinned_feature_names(estimator) == expected


# ---------------------------------------------------------------------------
# The counting projection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "problem_type", ["classification", "regression", "regression_ranking"]
)
def test_count_projection_reads_outcome_for_outcome_types(problem_type):
    assert "sub.outcome" in _count_projection(problem_type)


def test_count_projection_reads_the_survival_pair():
    projection = _count_projection("survival")

    assert "sub.duration" in projection
    assert "sub.event_observed" in projection


def test_count_projection_rejects_an_unknown_problem_type():
    with pytest.raises(ValueError, match="unknown problem_type"):
        _count_projection("clustering")


# ---------------------------------------------------------------------------
# The DB estimate — checked against what the builders really insert
# ---------------------------------------------------------------------------

_COHORT_QUERY = "select distinct entity_id from events where event_date < {as_of_date}"
# ``date {as_of_date}`` on purpose: an untyped literal next to ``+ interval`` makes PostgreSQL
# read the literal itself as an interval (adapters/labels module docstring).
_LABEL_QUERY = (
    "select entity_id, max(outcome) as outcome from events"
    " where event_date >= date {as_of_date}"
    " and event_date < date {as_of_date} + {label_timespan}"
    " group by entity_id"
)

_ESTIMATE_CONFIG = {
    "problem_type": "classification",
    "cohort_config": {"query": _COHORT_QUERY},
    "label_config": {"query": _LABEL_QUERY},
    "temporal_config": {
        "feature_start_time": "2013-01-01",
        "feature_end_time": "2015-01-01",
        "label_start_time": "2013-01-01",
        "label_end_time": "2015-01-01",
        "model_update_frequency": "6month",
        "training_as_of_date_frequencies": "6month",
        "test_as_of_date_frequencies": "6month",
        "max_training_histories": ["6month"],
        "test_durations": ["0day"],
        "training_label_timespans": ["6month"],
        "test_label_timespans": ["6month"],
    },
}


def _seed_events(pool):
    """Entities before 2014-01-01 → {1,2}; before 2014-07-01 → {1,2,3}."""
    with pool.connection() as conn:
        conn.execute(
            "create table events (entity_id bigint, event_date date, outcome int)"
        )
        conn.execute(
            "insert into events (entity_id, event_date, outcome) values"
            " (1, date '2013-06-01', 1), (2, date '2013-12-01', 0),"
            " (3, date '2014-03-01', 1), (1, date '2014-05-01', 0)"
        )


def _seed_lineage(pool):
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-preflight', '{}'::jsonb, 'classification')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile)"
            " values ('exp-preflight', 'local') returning run_id"
        ).fetchone()["run_id"]
    return str(run_id)


def test_estimate_matches_what_the_builders_actually_insert(db_pool_greenfield):
    """The load-bearing test: estimate the counts, then build for real and compare.

    An estimate is only worth printing if it equals the thing it estimates. Both halves render
    the same templates, so a divergence here means one of them stopped mirroring the other.
    """
    pool = db_pool_greenfield
    _seed_events(pool)
    run_id = _seed_lineage(pool)

    estimate = estimate_data(pool, _ESTIMATE_CONFIG)

    # Build over the SAME date union the estimate used — timechop's, not a hand-written list;
    # comparing different date sets would test the fixture, not the estimator.
    as_of_dates = [c.as_of_date for c in estimate.cohort]
    assert as_of_dates == sorted(set(as_of_dates)) and len(as_of_dates) > 1

    cohort_hash = build_cohort(
        pool,
        run_id,
        cohort_query_template=_COHORT_QUERY,
        as_of_dates=as_of_dates,
        config={"query": _COHORT_QUERY, "name": "c"},
        source_pins={"events": "v1"},
    )
    build_labels(
        pool,
        run_id,
        cohort_artifact_id=cohort_hash,
        label_query_template=_LABEL_QUERY,
        as_of_dates=as_of_dates,
        label_timespans=["6month"],
        config={"query": _LABEL_QUERY, "name": "label"},
        source_pins={"events": "v1"},
        problem_type="classification",
    )

    with pool.connection() as conn:
        actual_cohort = {
            row["as_of_date"]: row["n"]
            for row in conn.execute(
                "select as_of_date, count(distinct entity_id) as n from triage.cohorts"
                " where cohort_hash = %(h)s group by as_of_date",
                {"h": cohort_hash},
            ).fetchall()
        }
        actual_labels = {
            row["as_of_date"]: (row["n"], row["mean"])
            for row in conn.execute(
                "select as_of_date, count(distinct entity_id) as n,"
                "       avg(outcome::double precision) as mean"
                " from triage.labels group by as_of_date"
            ).fetchall()
        }

    # An as_of_date whose cohort is empty inserts NO rows, so the table has no group for it
    # while the estimate reports 0. Both are correct; compare with absent ≡ 0. (2013-01-01 is
    # such a date here — no events precede it — which is why the fixture keeps it.)
    estimated_cohort = {c.as_of_date: c.entities for c in estimate.cohort}
    assert estimated_cohort == {d: actual_cohort.get(d, 0) for d in estimated_cohort}
    assert estimated_cohort[min(estimated_cohort)] == 0, (
        "the empty-cohort date is the point"
    )

    for label in estimate.labels:
        if label.as_of_date not in actual_labels:
            continue
        n, mean = actual_labels[label.as_of_date]
        assert label.entities == n
        assert (label.outcome_mean is None) == (mean is None)
        if mean is not None:
            assert label.outcome_mean == pytest.approx(mean)


def test_estimate_can_sample_a_prefix_of_the_dates(db_pool_greenfield):
    """``max_dates`` bounds the cost on a config with many as_of_dates."""
    pool = db_pool_greenfield
    _seed_events(pool)

    full = estimate_data(pool, _ESTIMATE_CONFIG)
    sampled = estimate_data(pool, _ESTIMATE_CONFIG, max_dates=1)

    assert len(sampled.cohort) == 1
    assert len(full.cohort) > 1
    assert sampled.cohort[0] == full.cohort[0]


def test_estimate_requires_a_query_it_can_render(db_pool_greenfield):
    """A cohort_config with no query is a loud error, not an empty table."""
    config = dict(_ESTIMATE_CONFIG, cohort_config={"name": "no-query"})

    with pytest.raises(ValueError, match="cohort_config needs a 'query'"):
        estimate_data(db_pool_greenfield, config)


def test_model_groups_multiply_with_runs_but_matrices_do_not(fanout_config):
    """The two counts a fan-out treats differently — checked against a real run.

    A live 4-run x 4-split DirtyDuck fan-out recorded 12 model groups, 48 models and 8 matrix
    artifacts over 8 distinct Parquet files. Groups multiply because the feature list enters
    the group hash; matrices do not, because every subset is a column projection of the same
    Parquet (ADR-0023). Getting these backwards is what makes a fan-out look unaffordable.
    """
    plan = plan_experiment(fanout_config)

    assert plan.n_runs == 3
    assert plan.n_model_groups == plan.grid_size * plan.n_runs
    assert plan.n_matrices == 2 * plan.n_splits
    # The fan-out does not touch the matrix count at all.
    assert plan.n_matrices == plan_experiment(_no_fanout(fanout_config)).n_matrices


def _no_fanout(config):
    """The same config with its feature_groups removed."""
    stripped = yaml.safe_load(yaml.safe_dump(config))
    stripped["feature_config"].pop("feature_groups", None)
    return stripped


def test_counts_match_the_violations_experiment_as_it_actually_ran(dirtyduck_config):
    """example/dirtyduck/experiment-violations.yaml — 3 grid x 4 runs, live-verified.

    The database recorded 4 runs / 12 model groups / 48 models / 8 matrices for this config.
    Pinned here so a change to any of the three multiplications is caught.
    """
    config = yaml.safe_load(
        (_DIRTYDUCK.parent / "experiment-violations.yaml").read_text(encoding="utf-8")
    )

    plan = plan_experiment(config)

    assert plan.grid_size == 3
    assert plan.n_splits == 4
    assert plan.n_runs == 4
    assert plan.n_model_groups == 12
    assert plan.n_models == 48
    assert plan.n_matrices == 8


def test_plan_warns_when_a_split_would_produce_several_test_matrices():
    """``run._build_split`` raises on >1 test matrix per split — say so before the run does."""
    config = yaml.safe_load(_DIRTYDUCK.read_text(encoding="utf-8"))
    # Probed against timechop: it is *test_as_of_date_frequencies* that multiplies the test
    # matrices within a split. Extra test_durations / test_label_timespans add SPLITS instead,
    # each still holding one test matrix — which is why the warning keys on the per-split count
    # and not on any of those list lengths.
    config["temporal_config"]["test_as_of_date_frequencies"] = ["1month", "3month"]

    plan = plan_experiment(config)

    assert any("more than one test matrix" in w for w in plan.warnings), plan.warnings
    assert plan.n_test_matrices == 2 * plan.n_splits
