"""Validation of the in-Postgres metric functions (ADR-0007).

The ``0002_metric_functions`` migration creates PL/pgSQL functions that compute
precision@k / recall@k / AUC-ROC / average-precision / regression RMSE-MAE-R²
and SQL bias group-bys over the greenfield ``triage.*`` schema. This test is the
ADR-0007 proof that the SQL is correct: it stands up the greenfield schema via
alembic, inserts a small KNOWN fixture into models/matrices/predictions/labels/
protected_groups, calls the functions, and asserts the resulting
``triage.evaluations`` / ``triage.bias_metrics`` rows equal hand-computed (and
sklearn-computed) reference values.
"""

import json
import math

import pytest
from sklearn import metrics as skmetrics

from triage.component.catwalk.in_pg_evaluation import (
    compute_bias_in_db,
    evaluate_in_db,
)
from triage.component.results_schema import downgrade_db, upgrade_db

LABEL_TIMESPAN = "6 months"
AS_OF_DATE = "2014-01-01"


@pytest.fixture
def greenfield_engine(db_pool_greenfield):
    """Greenfield ``triage`` schema + the 0002 metric functions, via the shared
    ``db_pool_greenfield`` conftest fixture (alembic 0001 -> head), as a pool."""
    return db_pool_greenfield


# A small KNOWN fixture. 10 labeled entities. (score, outcome) pairs chosen so
# every metric has an unambiguous hand-computed value and the top-k cutoff falls
# on a clean boundary (no score ties across the k=5 line for the deterministic
# checks; a deliberate tie is added separately for the tie-bound check).
#
# entity: 1    2    3    4    5    6    7    8    9   10
# score : .95  .90  .85  .80  .75  .60  .50  .40  .30  .20
# label : 1    1    0    1    0    1    0    0    1    0
SCORES = [0.95, 0.90, 0.85, 0.80, 0.75, 0.60, 0.50, 0.40, 0.30, 0.20]
LABELS = [1, 1, 0, 1, 0, 1, 0, 0, 1, 0]
ENTITY_IDS = list(range(1, 11))


def _seed_model(pool):
    """Insert the lineage rows a prediction needs (artifact -> model_group ->
    model), returning the new model_id. Mirrors the 0001 FK chain."""
    with pool.connection() as conn:
        # experiment + run for built_by_run / provenance (nullable, but tidy)
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type) "
            "values ('exp1', '{}'::jsonb, 'classification')"
        )
        # model node artifact (models.model_hash references artifacts.artifact_id)
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('model-art-1', 'model-logical-1', 'model', '{}'::jsonb)"
        )
        conn.execute(
            "insert into triage.model_groups "
            "(model_group_hash, model_type, hyperparameters, feature_list) "
            "values ('mg1', 'sklearn.tree.DecisionTreeClassifier', '{}'::jsonb, "
            "ARRAY['f1','f2'])"
        )
        model_id = conn.execute(
            "insert into triage.models "
            "(model_group_id, model_hash, train_end_time) "
            "select model_group_id, 'model-art-1', date '2013-07-01' "
            "from triage.model_groups where model_group_hash = 'mg1' "
            "returning model_id"
        ).fetchone()["model_id"]
    return model_id


def _seed_predictions_and_labels(pool, model_id, scores, labels, entity_ids):
    with pool.connection() as conn:
        # labels node artifact (labels.label_hash references artifacts.artifact_id)
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('labels-art-1', 'labels-logical-1', 'labels', '{}'::jsonb)"
        )
        for eid, score, label in zip(entity_ids, scores, labels):
            conn.execute(
                "insert into triage.predictions "
                "(model_id, entity_id, as_of_date, split_kind, score) "
                "values (%(m)s, %(e)s, %(d)s, 'test', %(s)s)",
                {"m": model_id, "e": eid, "d": AS_OF_DATE, "s": score},
            )
            conn.execute(
                "insert into triage.labels "
                "(label_hash, entity_id, as_of_date, label_timespan, outcome) "
                "values ('labels-art-1', %(e)s, %(d)s, cast(%(ts)s as interval), %(o)s)",
                {"e": eid, "d": AS_OF_DATE, "ts": LABEL_TIMESPAN, "o": float(label)},
            )


def _eval_row(pool, model_id, metric, parameter=""):
    with pool.connection() as conn:
        row = conn.execute(
            "select value, value_worst, value_best, value_expected, value_std, "
            "num_labeled, num_positive from triage.evaluations "
            "where model_id = %(m)s and metric = %(metric)s and parameter = %(p)s "
            "and as_of_date = %(d)s",
            {"m": model_id, "metric": metric, "p": parameter, "d": AS_OF_DATE},
        ).fetchone()
    return row


# ----------------------------------------------------------------- classification


def test_precision_recall_at_k_absolute(greenfield_engine):
    """precision@5_abs / recall@5_abs vs hand + sklearn reference."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)

    n_written = evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={
            "metrics": ["precision@", "recall@", "auc_roc", "average_precision"],
            "thresholds": ["5_abs"],
        },
    )
    assert n_written == 4  # precision@5_abs, recall@5_abs, auc_roc, average_precision

    # Top-5 by score: entities 1..5, labels [1,1,0,1,0] -> 3 TP.
    top5_labels = LABELS[:5]
    n_positive = sum(LABELS)  # 5
    expected_precision = sum(top5_labels) / 5  # 3/5 = 0.6
    expected_recall = sum(top5_labels) / n_positive  # 3/5 = 0.6

    p = _eval_row(engine, model_id, "precision@", "5_abs")
    r = _eval_row(engine, model_id, "recall@", "5_abs")

    assert p["value"] == pytest.approx(expected_precision)
    assert r["value"] == pytest.approx(expected_recall)
    assert p["num_labeled"] == 10
    assert p["num_positive"] == n_positive
    assert r["num_labeled"] == 10

    # No score ties at the boundary -> deterministic worst == best == value.
    assert p["value_worst"] == pytest.approx(expected_precision)
    assert p["value_best"] == pytest.approx(expected_precision)

    # Random-ranking hypergeometric baseline for precision@k: expected = base rate.
    base_rate = n_positive / 10  # 0.5
    assert p["value_expected"] == pytest.approx(base_rate)
    k, N, K = 5, 10, n_positive
    expected_std = math.sqrt(k * base_rate * (1 - base_rate) * (N - k) / (N - 1)) / k
    assert p["value_std"] == pytest.approx(expected_std)

    # recall random baseline: expected = k/N.
    assert r["value_expected"] == pytest.approx(k / N)


def test_precision_recall_at_k_percentage(greenfield_engine):
    """precision@30_pct on 10 labeled rows -> ceil(0.30*10)=3 selected."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)

    evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["precision@", "recall@"], "thresholds": ["30_pct"]},
    )
    # Top-3 labels [1,1,0] -> 2 TP. precision = 2/3, recall = 2/5.
    p = _eval_row(engine, model_id, "precision@", "30_pct")
    r = _eval_row(engine, model_id, "recall@", "30_pct")
    assert p["value"] == pytest.approx(2 / 3)
    assert r["value"] == pytest.approx(2 / 5)


def test_auc_roc_matches_sklearn(greenfield_engine):
    """Mann-Whitney AUC in SQL == sklearn roc_auc_score."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)

    evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["auc_roc"], "thresholds": []},
    )
    expected_auc = skmetrics.roc_auc_score(LABELS, SCORES)
    auc = _eval_row(engine, model_id, "auc_roc", "")
    assert auc["value"] == pytest.approx(expected_auc)
    assert auc["value_worst"] is None  # scalar metric: no tie bounds
    assert auc["num_positive"] == sum(LABELS)


def test_auc_roc_with_score_ties_matches_sklearn(greenfield_engine):
    """AUC mid-rank handling for tied scores == sklearn."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    # introduce a score tie across the class boundary
    scores = [0.9, 0.8, 0.8, 0.8, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05]
    labels = [1, 1, 0, 1, 0, 1, 0, 0, 1, 0]
    _seed_predictions_and_labels(engine, model_id, scores, labels, ENTITY_IDS)

    evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["auc_roc"], "thresholds": []},
    )
    expected_auc = skmetrics.roc_auc_score(labels, scores)
    auc = _eval_row(engine, model_id, "auc_roc", "")
    assert auc["value"] == pytest.approx(expected_auc)


def test_average_precision_matches_sklearn(greenfield_engine):
    """Average precision (PR-AUC) in SQL == sklearn average_precision_score."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)

    evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["average_precision"], "thresholds": []},
    )
    expected_ap = skmetrics.average_precision_score(LABELS, SCORES)
    ap = _eval_row(engine, model_id, "average_precision", "")
    assert ap["value"] == pytest.approx(expected_ap)


def test_precision_at_k_tie_bounds(greenfield_engine):
    """When the top-k boundary falls inside a score-tie block, value_worst /
    value_best bracket the deterministic realized value."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    scores = [0.95, 0.80, 0.80, 0.80, 0.50, 0.40, 0.30, 0.20, 0.10, 0.05]
    labels = [1, 1, 0, 1, 0, 0, 0, 0, 0, 0]
    _seed_predictions_and_labels(engine, model_id, scores, labels, ENTITY_IDS)

    evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["precision@"], "thresholds": ["2_abs"]},
    )
    p = _eval_row(engine, model_id, "precision@", "2_abs")
    assert p["value_worst"] == pytest.approx(0.5)
    assert p["value_best"] == pytest.approx(1.0)
    # deterministic realized value sits within [worst, best]
    assert p["value_worst"] <= p["value"] <= p["value_best"]


# --------------------------------------------------------------------- regression


def test_regression_metrics_match_sklearn(greenfield_engine):
    """RMSE / MAE / R² in SQL == sklearn / numpy reference."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    # score = prediction, outcome = actual (continuous target, ADR-0010).
    preds = [2.5, 0.0, 2.0, 8.0, 1.5, 3.0, 5.5, 7.0, 4.0, 6.0]
    actual = [3.0, -0.5, 2.0, 7.0, 1.0, 2.5, 6.0, 6.5, 4.5, 5.5]
    _seed_predictions_and_labels(engine, model_id, preds, actual, ENTITY_IDS)

    evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"regression_metrics": ["rmse", "mae", "r2"]},
    )
    expected_rmse = math.sqrt(skmetrics.mean_squared_error(actual, preds))
    expected_mae = skmetrics.mean_absolute_error(actual, preds)
    expected_r2 = skmetrics.r2_score(actual, preds)

    rmse = _eval_row(engine, model_id, "rmse", "")
    mae = _eval_row(engine, model_id, "mae", "")
    r2 = _eval_row(engine, model_id, "r2", "")
    assert rmse["value"] == pytest.approx(expected_rmse)
    assert mae["value"] == pytest.approx(expected_mae)
    assert r2["value"] == pytest.approx(expected_r2)
    # scalar regression metrics carry no tie bounds / positive count
    assert rmse["value_worst"] is None
    assert rmse["num_positive"] is None
    assert rmse["num_labeled"] == 10


# ------------------------------------------------------------------------- bias


def _seed_protected_groups(pool, entity_ids, groups, attribute_name="race"):
    with pool.connection() as conn:
        for eid, g in zip(entity_ids, groups):
            conn.execute(
                "insert into triage.protected_groups "
                "(entity_id, as_of_date, attribute_name, attribute_value) "
                "values (%(e)s, %(d)s, %(a)s, %(v)s)",
                {"e": eid, "d": AS_OF_DATE, "a": attribute_name, "v": g},
            )


def test_bias_metrics_group_by_and_disparity(greenfield_engine):
    """SQL bias group-by computes per-group selection rate + disparity vs the
    reference (largest) group, matching hand computation."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)
    groups = ["A", "A", "A", "A", "A", "A", "B", "B", "B", "B"]
    _seed_protected_groups(engine, ENTITY_IDS, groups)

    n = compute_bias_in_db(
        engine, model_id, AS_OF_DATE, LABEL_TIMESPAN, parameter="5_abs"
    )
    assert n > 0

    with engine.connection() as conn:
        rows = conn.execute(
            "select attribute_value, value, ref_group_value, disparity "
            "from triage.bias_metrics "
            "where model_id = %(m)s and metric = 'selection_rate' "
            "and attribute_name = 'race' order by attribute_value",
            {"m": model_id},
        ).fetchall()
    by_group = {row["attribute_value"]: row for row in rows}
    assert by_group["A"]["value"] == pytest.approx(5 / 6)
    assert by_group["B"]["value"] == pytest.approx(0.0)
    # reference group is the larger one, A
    assert by_group["A"]["ref_group_value"] == "A"
    assert by_group["B"]["ref_group_value"] == "A"
    # disparity of A vs itself == 1.0; B vs A == 0.
    assert by_group["A"]["disparity"] == pytest.approx(1.0)
    assert by_group["B"]["disparity"] == pytest.approx(0.0)

    # group_size is a count -> no disparity
    with engine.connection() as conn:
        gsize = conn.execute(
            "select attribute_value, value, disparity from triage.bias_metrics "
            "where model_id = %(m)s and metric = 'group_size' "
            "and attribute_name = 'race' order by attribute_value",
            {"m": model_id},
        ).fetchall()
    gmap = {r["attribute_value"]: r for r in gsize}
    assert gmap["A"]["value"] == pytest.approx(6)
    assert gmap["B"]["value"] == pytest.approx(4)
    assert gmap["A"]["disparity"] is None


def test_bias_metrics_explicit_reference_group(greenfield_engine):
    """A caller-supplied reference group overrides the largest-group default."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)
    groups = ["A", "A", "A", "A", "A", "A", "B", "B", "B", "B"]
    _seed_protected_groups(engine, ENTITY_IDS, groups)

    compute_bias_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        parameter="5_abs",
        ref_groups={"race": "B"},
    )
    with engine.connection() as conn:
        rows = conn.execute(
            "select attribute_value, ref_group_value from triage.bias_metrics "
            "where model_id = %(m)s and metric = 'selection_rate' "
            "and attribute_name = 'race'",
            {"m": model_id},
        ).fetchall()
    for row in rows:
        assert row["ref_group_value"] == "B"


def test_bias_metrics_full_confusion_set_and_tau(greenfield_engine):
    """Migration 0014: fnr / for / npv match hand computation, the zero-reference
    disparity guard holds, and the SQL fairness verdict follows τ.

    k=5 cut over SCORES: selected = e1..e5. Group A = e1..e6, B = e7..e10.
      A: fn=1 (e6), tn=0, pos=4, not-selected=1 -> fnr=1/4, for=1/1, npv=0/1
      B: fn=1 (e9), tn=3, pos=1, not-selected=4 -> fnr=1/1, for=1/4, npv=3/4
    Reference = A (largest): fnr disparity B = 4.0; for disparity B = 0.25;
    npv reference value is 0.0 -> npv disparity is NULL for every group (guard).
    """
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)
    groups = ["A", "A", "A", "A", "A", "A", "B", "B", "B", "B"]
    _seed_protected_groups(engine, ENTITY_IDS, groups)

    compute_bias_in_db(engine, model_id, AS_OF_DATE, LABEL_TIMESPAN, parameter="5_abs")

    def _rows(metric):
        with engine.connection() as conn:
            rows = conn.execute(
                "select attribute_value, value, disparity, tau, passes_fairness "
                "from triage.bias_metrics "
                "where model_id = %(m)s and metric = %(metric)s "
                "and attribute_name = 'race' order by attribute_value",
                {"m": model_id, "metric": metric},
            ).fetchall()
        return {r["attribute_value"]: r for r in rows}

    fnr = _rows("fnr")
    assert fnr["A"]["value"] == pytest.approx(0.25)
    assert fnr["B"]["value"] == pytest.approx(1.0)
    assert fnr["B"]["disparity"] == pytest.approx(4.0)
    assert fnr["A"]["passes_fairness"] is True  # disparity 1.0 vs itself
    assert fnr["B"]["passes_fairness"] is False  # 4.0 outside [0.8, 1.25]
    assert fnr["B"]["tau"] == pytest.approx(0.8)

    for_rate = _rows("for")
    assert for_rate["A"]["value"] == pytest.approx(1.0)
    assert for_rate["B"]["value"] == pytest.approx(0.25)
    assert for_rate["B"]["disparity"] == pytest.approx(0.25)
    assert for_rate["B"]["passes_fairness"] is False

    npv = _rows("npv")
    assert npv["A"]["value"] == pytest.approx(0.0)
    assert npv["B"]["value"] == pytest.approx(0.75)
    # reference (A) npv is 0.0 -> disparity undefined for the whole attribute
    assert npv["A"]["disparity"] is None
    assert npv["B"]["disparity"] is None
    assert npv["B"]["passes_fairness"] is None  # no verdict without a disparity

    # counts carry neither τ nor a verdict
    gsize = _rows("group_size")
    assert gsize["A"]["tau"] is None
    assert gsize["A"]["passes_fairness"] is None

    # τ is config, not constant: at τ=0.2 the band is [0.2, 5.0] — B's 'for'
    # disparity (0.25) now passes, and the idempotent upsert updates the verdict.
    compute_bias_in_db(
        engine, model_id, AS_OF_DATE, LABEL_TIMESPAN, parameter="5_abs", tau=0.2
    )
    for_rate = _rows("for")
    assert for_rate["B"]["tau"] == pytest.approx(0.2)
    assert for_rate["B"]["passes_fairness"] is True
    fnr = _rows("fnr")
    assert fnr["B"]["passes_fairness"] is True  # 4.0 sits inside [0.2, 5.0]


# ------------------------------------------------------------------ idempotency


def test_evaluate_model_is_idempotent(greenfield_engine):
    """Re-running evaluate_model upserts in place (no duplicate PK rows)."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)

    cfg = {"metrics": ["precision@", "auc_roc"], "thresholds": ["5_abs"]}
    evaluate_in_db(engine, model_id, AS_OF_DATE, LABEL_TIMESPAN, metric_config=cfg)
    evaluate_in_db(engine, model_id, AS_OF_DATE, LABEL_TIMESPAN, metric_config=cfg)

    with engine.connection() as conn:
        count = conn.execute(
            "select count(*) as n from triage.evaluations where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()["n"]
    # 2 metrics (precision@5_abs, auc_roc) -> 2 rows even after two runs.
    assert count == 2


def test_empty_labeled_set_is_safe(greenfield_engine):
    """A model with predictions but no matching labels writes count-only rows
    rather than dividing by zero."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    # predictions only, no labels at this timespan
    with engine.connection() as conn:
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) "
            "values ('labels-art-1', 'labels-logical-1', 'labels', '{}'::jsonb)"
        )
        for eid, score in zip(ENTITY_IDS, SCORES):
            conn.execute(
                "insert into triage.predictions "
                "(model_id, entity_id, as_of_date, split_kind, score) "
                "values (%(m)s, %(e)s, %(d)s, 'test', %(s)s)",
                {"m": model_id, "e": eid, "d": AS_OF_DATE, "s": score},
            )

    evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["precision@"], "thresholds": ["5_abs"]},
    )
    p = _eval_row(engine, model_id, "precision@", "5_abs")
    assert p["num_labeled"] == 0
    assert p["value"] is None


# ----------------------------------------------- live telemetry (read-dashboard §4)


def _link_model_to_run(pool, model_id):
    """Create a run row and point the model's ``run_id`` at it; returns run_id (str).

    ``_seed_model`` leaves ``models.run_id`` null (no run); the evaluation NOTIFY is
    only emitted when a run owns the model, so wire one up for the emit-path test."""
    with pool.connection() as conn:
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile, status) "
            "values ('exp1', 'local', 'started') returning run_id"
        ).fetchone()["run_id"]
        conn.execute(
            "update triage.models set run_id = %(r)s where model_id = %(m)s",
            {"r": run_id, "m": model_id},
        )
    return str(run_id)


def test_evaluate_in_db_emits_evaluation_completed(greenfield_engine):
    """evaluate_in_db emits evaluation/completed on the run_progress channel for a
    model owned by a run, on the same COMMIT as the evaluation rows."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)
    run_id = _link_model_to_run(engine, model_id)

    listener = engine.getconn()
    listener.execute("listen run_progress")
    listener.commit()
    try:
        evaluate_in_db(
            engine,
            model_id,
            AS_OF_DATE,
            LABEL_TIMESPAN,
            metric_config={"metrics": ["precision@"], "thresholds": ["5_abs"]},
        )
        payloads = [
            json.loads(note.payload)
            for note in listener.notifies(timeout=5.0, stop_after=1)
        ]
    finally:
        engine.putconn(listener)

    assert payloads == [{"run_id": run_id, "kind": "evaluation", "status": "completed"}]


def test_evaluate_in_db_no_run_does_not_emit_or_error(greenfield_engine):
    """A model with no owning run (run_id null) evaluates fine and emits nothing —
    the NOTIFY is guarded on a non-null run_id."""
    engine = greenfield_engine
    model_id = _seed_model(engine)  # leaves models.run_id null
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)

    listener = engine.getconn()
    listener.execute("listen run_progress")
    listener.commit()
    try:
        written = evaluate_in_db(
            engine,
            model_id,
            AS_OF_DATE,
            LABEL_TIMESPAN,
            metric_config={"metrics": ["precision@"], "thresholds": ["5_abs"]},
        )
        payloads = [
            json.loads(note.payload)
            for note in listener.notifies(timeout=1.0, stop_after=1)
        ]
    finally:
        engine.putconn(listener)

    assert written == 1  # evaluation still happened
    assert payloads == []  # but no NOTIFY (no run owns the model)


def test_evaluate_in_db_no_listener_is_safe(greenfield_engine):
    """evaluate_in_db with a run-owned model but NO listener must not error."""
    engine = greenfield_engine
    model_id = _seed_model(engine)
    _seed_predictions_and_labels(engine, model_id, SCORES, LABELS, ENTITY_IDS)
    _link_model_to_run(engine, model_id)

    written = evaluate_in_db(
        engine,
        model_id,
        AS_OF_DATE,
        LABEL_TIMESPAN,
        metric_config={"metrics": ["precision@"], "thresholds": ["5_abs"]},
    )
    assert written == 1


# ------------------------------------------------------------------- migration


def test_0002_downgrade_then_reupgrade(db_url, db_pool):
    """0002 downgrade drops the functions + type cleanly, and re-upgrade
    rebuilds them (no leftover objects blocking the re-create)."""
    upgrade_db(dburl=db_url, revision="head")

    def fn_count(conn):
        return conn.execute(
            "select count(*) as n from pg_proc p "
            "join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = 'triage'"
        ).fetchone()["n"]

    with db_pool.connection() as conn:
        assert fn_count(conn) > 0  # functions exist after head

    # downgrade 0002 -> 0001 removes the functions + composite type
    downgrade_db(dburl=db_url, revision="0001_initial_triage_schema")
    with db_pool.connection() as conn:
        assert fn_count(conn) == 0
        # the base schema from 0001 survives
        assert (
            conn.execute("select to_regclass('triage.evaluations') as r").fetchone()[
                "r"
            ]
            is not None
        )

    # re-upgrade rebuilds them
    upgrade_db(dburl=db_url, revision="head")
    with db_pool.connection() as conn:
        assert fn_count(conn) > 0
