"""Model structure (diagnostics/structure.py): leaf support, the linear mean shift, families.

Small scikit-learn models are fitted here and stored the way the model path stores them: a
joblib file at ``models.artifact_uri``, the train and test matrices as Parquet rows of
``triage.matrices``, and the test scores in ``triage.predictions``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import polars as pl
import pytest
from typer.testing import CliRunner

import triage.cli as cli
from triage.diagnostics.structure import (
    StructureUnavailable,
    family_of,
    importance_by_family,
    leaf_support,
    mean_shift,
    sensitivity,
)

FEATURES = ["COUNT(inspections.date|interval=P1M)", "facilities.zip_code=60622", "x"]
TEST_DATE = "2015-01-01"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: Twenty column names from the DirtyDuck experiments' featurizer manifest, and their family.
DIRTYDUCK = {
    "ABS(facilities.COUNT(inspections.date))": "inspections.date",
    "ABS(facilities.COUNT(inspections.date|interval=P1M))": "inspections.date",
    "ABS(facilities.COUNT(inspections.result|interval=P3M))": "inspections.result",
    "ABS(facilities.NUNIQUE(inspections.risk|interval=P6M))": "inspections.risk",
    "ABS(facilities.RECENCY(inspections.date))": "inspections.date",
    "ABS(facilities.TENURE(inspections.date|interval=P6M))": "inspections.date",
    "COUNT(inspections.date)": "inspections.date",
    "COUNT(inspections.risk|interval=P1M)": "inspections.risk",
    "COUNT(inspections.type|interval=P6M)": "inspections.type",
    "NUNIQUE(inspections.result)": "inspections.result",
    "NUNIQUE(inspections.type|interval=P3M)": "inspections.type",
    "RECENCY(inspections.date)": "inspections.date",
    "RECENCY(inspections.date|interval=P3M)": "inspections.date",
    "TENURE(inspections.date|interval=P1M)": "inspections.date",
    "facilities.facility_type=bakery": "facilities.facility_type",
    "facilities.facility_type=daycare (2 - 6 years)": "facilities.facility_type",
    "facilities.facility_type=children's services facility": "facilities.facility_type",
    "facilities.zip_code=60607": "facilities.zip_code",
    "facilities.zip_code=60622": "facilities.zip_code",
    "COUNT(inspections.risk|interval=P6M)__missing": "inspections.risk",
}


def test_family_of_real_featurizer_names():
    assert {name: family_of(name) for name in DIRTYDUCK} == DIRTYDUCK


def test_family_of_resolves_a_truncated_name_through_the_manifest():
    labels = {
        "COUNT(inspections.date|interval=P~67a3dcf5": "COUNT(inspections.date|interval=P10Y)"
    }
    assert family_of("COUNT(inspections.date|interval=P~67a3dcf5", labels) == (
        "inspections.date"
    )


# ------------------------------------------------------------------ leaf support


def test_a_planted_one_row_leaf_is_reported():
    from sklearn.tree import DecisionTreeRegressor

    x = np.array([[float(i % 10)] for i in range(60)] + [[100.0]])
    y = np.array([float(i % 10) for i in range(60)] + [999.0])
    tree = DecisionTreeRegressor(max_depth=3, min_samples_leaf=1, random_state=0)
    support = leaf_support(tree.fit(x, y), min_rows=2)
    assert support["n_trees"] == 1
    assert support["small_leaves"] >= 1
    assert support["small"][0] == {
        "tree": 0,
        "node": support["small"][0]["node"],
        "n_rows": 1,
        "value": 999.0,
    }


def test_leaf_support_reads_forests_and_boosting():
    from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor

    rng = np.random.default_rng(0)
    x, y = rng.random((80, 2)), rng.random(80)
    forest = leaf_support(
        RandomForestRegressor(n_estimators=3, random_state=0).fit(x, y)
    )
    assert forest["n_trees"] == 3 and forest["estimator"] == "RandomForestRegressor"
    boosted = leaf_support(
        GradientBoostingRegressor(n_estimators=4, max_depth=2, random_state=0).fit(x, y)
    )
    assert boosted["n_trees"] == 4 and boosted["leaves_per_tree"]["max"] <= 4


def test_histogram_boosting_is_unsupported():
    from sklearn.ensemble import HistGradientBoostingRegressor

    rng = np.random.default_rng(0)
    model = HistGradientBoostingRegressor(max_iter=3).fit(
        rng.random((50, 2)), rng.random(50)
    )
    with pytest.raises(StructureUnavailable, match="not available for HistGradient"):
        leaf_support(model)


# ------------------------------------------------------------------ the stored model


def _store(pool, tmp_path: Path, estimator, x_train, x_test, tag: str = "s") -> int:
    """A regression model with its estimator, both matrices and its test scores."""
    from triage.adapters.model import as_uuid

    uris = {}
    for kind, x in (("train", x_train), ("test", x_test)):
        frame = pl.DataFrame(
            {
                "entity_id": list(range(1, len(x) + 1)),
                "as_of_date": [TEST_DATE] * len(x),
            }
            | {f: x[:, j] for j, f in enumerate(FEATURES)}
        ).with_columns(pl.col("as_of_date").str.to_date())
        uris[kind] = str(tmp_path / f"{tag}-{kind}.parquet")
        frame.write_parquet(uris[kind])
    artifact = str(tmp_path / f"{tag}.joblib")
    joblib.dump(estimator, artifact)
    scores = estimator.predict(x_test)
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values (%(h)s, '{}'::jsonb, 'regression') on conflict do nothing",
            {"h": "exp-structure"},
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile)"
            " values ('exp-structure', 'local') returning run_id"
        ).fetchone()["run_id"]
        for kind in ("train", "test"):
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, 'matrix', '{}'::jsonb)",
                {"a": f"{tag}-mx-{kind}"},
            )
            conn.execute(
                "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
                " storage_uri, feature_names, label_timespan) values (%(u)s::uuid,"
                " %(a)s, %(k)s, %(uri)s, %(f)s, interval '1 month')",
                {
                    "u": str(as_uuid(f"{tag}-mx-{kind}")),
                    "a": f"{tag}-mx-{kind}",
                    "k": kind,
                    "uri": uris[kind],
                    "f": FEATURES,
                },  # fmt: skip
            )
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
            {"a": f"{tag}-model"},
        )
        gid = conn.execute(
            "insert into triage.model_groups (model_group_hash, model_type,"
            " hyperparameters, feature_list) values (%(h)s, %(t)s, '{}'::jsonb, %(f)s)"
            " returning model_group_id",
            {"h": f"{tag}-group", "t": type(estimator).__name__, "f": FEATURES},
        ).fetchone()["model_group_id"]
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, run_id,"
            " train_matrix_uuid, train_end_time, artifact_uri, feature_list)"
            " values (%(g)s, %(a)s, %(r)s, %(u)s::uuid, date '2014-12-01', %(uri)s, %(f)s)"
            " returning model_id",
            {"g": gid, "a": f"{tag}-model", "r": run_id,
             "u": str(as_uuid(f"{tag}-mx-train")), "uri": artifact, "f": FEATURES},
        ).fetchone()["model_id"]  # fmt: skip
        with conn.cursor() as cur:
            cur.executemany(
                "insert into triage.predictions (model_id, entity_id, as_of_date,"
                " split_kind, score, matrix_uuid) values (%(m)s, %(e)s, %(d)s, 'test',"
                " %(s)s, %(u)s::uuid)",
                [
                    {
                        "m": model_id,
                        "e": e,
                        "d": TEST_DATE,
                        "s": float(s),
                        "u": str(as_uuid(f"{tag}-mx-test")),
                    }
                    for e, s in enumerate(scores, start=1)
                ],  # fmt: skip
            )
    return model_id


def _ridge_design():
    from sklearn.linear_model import Ridge

    rng = np.random.default_rng(3)
    x_train = rng.normal(size=(300, 3))
    y = x_train @ np.array([2.0, -1.0, 0.5]) + 4.0 + rng.normal(scale=0.1, size=300)
    x_test = rng.normal(size=(200, 3)) + np.array(
        [1.0, 0.5, -2.0]
    )  # a known mean shift
    return Ridge(alpha=1.0).fit(x_train, y), x_train, x_test


def test_the_ridge_decomposition_reproduces_the_stored_mean(
    db_pool_greenfield, tmp_path
):
    ridge, x_train, x_test = _ridge_design()
    model_id = _store(db_pool_greenfield, tmp_path, ridge, x_train, x_test)
    shift = mean_shift(db_pool_greenfield, model_id)
    assert shift["check_error"] < 1e-6
    assert shift["mean_forecast_test"] == pytest.approx(
        ridge.predict(x_test).mean(), abs=1e-6
    )
    assert shift["mean_forecast_train"] == pytest.approx(
        ridge.predict(x_train).mean(), abs=1e-6
    )
    assert shift["shift"] == pytest.approx(
        shift["mean_forecast_test"] - shift["mean_forecast_train"], abs=1e-9
    )
    families = {r["family"] for r in shift["families"]}
    assert families == {"inspections.date", "facilities.zip_code", "x"}
    expected = ridge.coef_ * (x_test.mean(axis=0) - x_train.mean(axis=0))
    by_feature = {r["feature"]: r["contribution"] for r in shift["features"]}
    assert [by_feature[f] for f in FEATURES] == pytest.approx(expected)


def test_a_classifier_is_not_decomposed(db_pool_greenfield, tmp_path):
    from sklearn.linear_model import LogisticRegression

    rng = np.random.default_rng(1)
    model = LogisticRegression().fit(rng.normal(size=(40, 3)), [0, 1] * 20)
    with pytest.raises(StructureUnavailable, match="forecasts through a link"):
        mean_shift(db_pool_greenfield, 1, model)


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def run_cli(db_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def run(*args: str):
        result = CliRunner().invoke(
            cli.app, list(args), env={"DATABASE_URL": db_url, "COLUMNS": "200"}
        )
        return result.exit_code, " ".join(_ANSI.sub("", result.output).split())

    return run


def test_structure_cli(run_cli, db_pool_greenfield, tmp_path):
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.tree import DecisionTreeRegressor

    ridge, x_train, x_test = _ridge_design()
    linear = _store(db_pool_greenfield, tmp_path, ridge, x_train, x_test, "lin")
    code, text = run_cli("postmodel", "structure", str(linear))
    assert code == 0, text
    assert "Mean shift by feature family" in text and "inspections.date" in text
    tree = DecisionTreeRegressor(max_depth=4, min_samples_leaf=1, random_state=0)
    planted = _store(
        db_pool_greenfield, tmp_path, tree.fit(x_train, x_train[:, 0]), x_train, x_test,
        "tree",
    )  # fmt: skip
    code, text = run_cli("postmodel", "structure", str(planted), "--json")
    assert code == 0, text
    payload = json.loads(text[text.index("{") :])
    assert payload["leaf_support"]["estimator"] == "DecisionTreeRegressor"
    hist = HistGradientBoostingRegressor(max_iter=3).fit(x_train, x_train[:, 0])
    opaque = _store(db_pool_greenfield, tmp_path, hist, x_train, x_test, "hist")
    code, text = run_cli("postmodel", "structure", str(opaque))
    assert code == 1
    assert "leaf support not available for HistGradientBoostingRegressor" in text


# ------------------------------------------------------------------ importance, sensitivity


def _seed_groups(pool, hyperparameters: list[dict[str, Any]]) -> list[int]:
    """Ridge groups of one Experiment with the given hyperparameters, one model each, an
    MAE evaluation of 1 + alpha on two dates and two feature importances."""
    ids = []
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-sens', '{}'::jsonb, 'regression')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile)"
            " values ('exp-sens', 'local') returning run_id"
        ).fetchone()["run_id"]
        for i, hp in enumerate(hyperparameters):
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
                {"a": f"sens-{i}"},
            )
            gid = conn.execute(
                "insert into triage.model_groups (model_group_hash, model_type,"
                " hyperparameters, feature_list) values (%(h)s,"
                " 'sklearn.linear_model.Ridge', %(hp)s::jsonb, array['a', 'b'])"
                " returning model_group_id",
                {"h": f"sens-g{i}", "hp": json.dumps(hp)},
            ).fetchone()["model_group_id"]
            for j, end in enumerate(("2014-06-01", "2014-12-01")):
                conn.execute(
                    "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                    " values (%(a)s, %(a)s, 'model', '{}'::jsonb) on conflict do nothing",
                    {"a": f"sens-{i}-{j}"},
                )
                mid = conn.execute(
                    "insert into triage.models (model_group_id, model_hash, run_id,"
                    " train_end_time) values (%(g)s, %(a)s, %(r)s, %(e)s)"
                    " returning model_id",
                    {"g": gid, "a": f"sens-{i}-{j}", "r": run_id, "e": end},
                ).fetchone()["model_id"]
                for d in ("2015-01-01", "2015-02-01"):
                    conn.execute(
                        "insert into triage.evaluations (model_id, split_kind, as_of_date,"
                        " metric, parameter, value) values (%(m)s, 'test', %(d)s, 'mae',"
                        " '', %(v)s)",
                        {"m": mid, "d": d, "v": 1.0 + float(hp.get("alpha", 0))},
                    )
                conn.execute(
                    "insert into triage.feature_importances (model_id, feature,"
                    " feature_importance) values (%(m)s, 'COUNT(inspections.date)', 3.0),"
                    " (%(m)s, %(f)s, 1.0)",
                    {
                        "m": mid,
                        "f": "facilities.zip_code=60622" if j else "RECENCY(x.y)",
                    },
                )
            ids.append(gid)
    return ids


def test_sensitivity_by_one_hyperparameter(db_pool_greenfield):
    ids = _seed_groups(db_pool_greenfield, [{"alpha": a, "fit_intercept": True}
                                            for a in (0.1, 1.0, 10.0)])  # fmt: skip
    result = sensitivity(db_pool_greenfield, "exp-sens", "alpha")
    assert [(r["value"], r["model_group_id"]) for r in result["rows"]] == list(
        zip((0.1, 1.0, 10.0), ids, strict=True)
    )
    assert [r["mean_metric"] for r in result["rows"]] == pytest.approx([1.1, 2.0, 11.0])


def test_sensitivity_refuses_groups_that_differ_twice(db_pool_greenfield):
    _seed_groups(
        db_pool_greenfield,
        [{"alpha": 0.1, "fit_intercept": True}, {"alpha": 1.0, "fit_intercept": False}],
    )
    with pytest.raises(ValueError, match="differ in fit_intercept as well as 'alpha'"):
        sensitivity(db_pool_greenfield, "exp-sens", "alpha")


def test_importance_by_family_and_its_stability(db_pool_greenfield):
    (gid,) = _seed_groups(db_pool_greenfield, [{"alpha": 1.0}])
    result = importance_by_family(db_pool_greenfield, gid, top=1)
    first, second = result["models"]
    assert first["families"] == pytest.approx({"inspections.date": 0.75, "x.y": 0.25})
    assert second["families"] == pytest.approx(
        {"inspections.date": 0.75, "facilities.zip_code": 0.25}
    )
    assert result["stability"][0]["jaccard"] == 1.0  # the top feature did not move


def test_importance_and_sensitivity_cli(run_cli, db_pool_greenfield, tmp_path):
    ids = _seed_groups(db_pool_greenfield, [{"alpha": a} for a in (0.1, 1.0)])
    code, text = run_cli("postmodel", "importance", str(ids[0]))
    assert code == 0 and "inspections.date" in text and "top-5 Jaccard" in text, text
    code, text = run_cli("postmodel", "sensitivity", "exp-sens", "--param", "alpha")
    assert code == 0 and "mae by alpha" in text, text
    code, text = run_cli("postmodel", "sensitivity", "exp-sens", "--param", "nope")
    assert code == 2 and "sets 'nope'" in text
    code, text = run_cli("plot", "importance", str(ids[0]))
    assert code == 0 and f"wrote importance-{ids[0]}.svg" in text, text
    assert (tmp_path / f"importance-{ids[0]}.svg").exists()
