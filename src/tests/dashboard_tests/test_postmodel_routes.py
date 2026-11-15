"""The postmodel panels' routes, one problem type at a time (plan Phase 13).

Each route wraps a reader of :mod:`triage.diagnostics` or a diagnostics module, so the tests
check three things: the route answers the reader's rows unchanged, an empty source answers
the empty-state envelope whose hint names the CLI command, and a model of a problem type the
diagnostic does not read answers 422 naming the command that does. The fixtures are the
seeds of the phase tests that built each reader (regression residuals, the baseline
comparison, allocation metrics, survival, near miss and prototypes, structure, common rows).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from tests.test_allocation_metrics import R1, seed_allocation
from tests.test_baseline_compare import EXP as BASELINE_EXP
from tests.test_baseline_compare import _seed as seed_baseline
from tests.test_common_rows import EXP as COMMON_EXP
from tests.test_common_rows import _seed as seed_common_rows
from tests.test_diagnostics import REG_DATES, _seed_regression_trees
from tests.test_near_miss_prototypes import seed_model as seed_near_miss
from tests.test_regression_diagnostics import _seed as seed_regression
from tests.test_structure import _ridge_design
from tests.test_structure import _store as store_estimator
from tests.test_survival_diagnostics import H, TIMES, seed_survival
from triage.component.registry_schema import upgrade_registry_db
from triage.dashboard.app import create_app
from triage.dashboard.auth import TrustedHeaderAuth
from triage.diagnostics import readers


def _instants(value: Any) -> Any:
    """``value`` with every timestamp string parsed: an encoder may write UTC as ``Z`` or
    as ``+00:00`` (CI runs in UTC), and the two are one instant."""
    if isinstance(value, dict):
        return {k: _instants(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_instants(v) for v in value]
    if isinstance(value, str) and "T" in value and value[:4].isdigit():
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def _same(route_body: Any, reader_rows: Any) -> None:
    """The route's JSON is the reader's rows, encoded the way FastAPI encodes them."""
    expected = json.loads(json.dumps(jsonable_encoder(reader_rows)))
    assert _instants(route_body) == _instants(expected)


def _model_of(pool, group_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "select min(model_id) as m from triage.models where model_group_id = %(g)s",
            {"g": group_id},
        ).fetchone()["m"]


def _unscored_regression_model(pool) -> int:
    """A regression model with no prediction: every computed-on-read panel is empty."""
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values ('exp-unscored', '{}'::jsonb, 'regression')"
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile)"
            " values ('exp-unscored', 'local') returning run_id"
        ).fetchone()["run_id"]
        gid = conn.execute(
            "insert into triage.model_groups (model_group_hash, model_type,"
            " hyperparameters, feature_list) values ('unscored',"
            " 'sklearn.linear_model.Ridge', '{}'::jsonb, array['f'])"
            " returning model_group_id"
        ).fetchone()["model_group_id"]
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
            " values ('unscored-model', 'unscored-model', 'model', '{}'::jsonb)"
        )
        return conn.execute(
            "insert into triage.models (model_group_id, model_hash, run_id,"
            " train_end_time, training_label_timespan) values (%(g)s, 'unscored-model',"
            " %(r)s, date '2015-01-01', interval '1 month') returning model_id",
            {"g": gid, "r": run_id},
        ).fetchone()["model_id"]


# ------------------------------------------------------------------- the model card


def test_the_model_card_names_its_problem_type(client, seeded, db_pool_greenfield):
    classifier = seeded.latest_model["mg1"]
    assert client.get(f"/api/models/{classifier}").json()["problem_type"] == (
        "classification"
    )
    regression = seed_regression(db_pool_greenfield)
    assert client.get(f"/api/models/{regression}").json()["problem_type"] == (
        "regression"
    )


# ------------------------------------------------------------------- the type gates


@pytest.mark.parametrize(
    ("path", "seed", "named"),
    [
        ("residuals", "classification", "triage plot calibration"),
        ("residuals/segments", "survival", "triage postmodel survival"),
        ("forecast-actual", "classification", "triage plot calibration"),
        ("gains", "survival", "triage postmodel survival"),
        ("survival", "classification", "triage plot calibration"),
        ("survival", "regression", "triage postmodel residuals"),
        ("near-miss", "regression", "triage plot gains"),
        ("prototypes", "regression", "triage postmodel crosstabs"),
        ("prototypes", "survival", "--horizon H"),
        ("baseline", "classification", "triage postmodel compare"),
    ],
)
def test_a_wrong_type_is_refused_with_the_command_that_applies(
    client, db_pool_greenfield, tmp_path, path, seed, named
):
    pool = db_pool_greenfield
    if seed == "classification":
        model_id = seed_near_miss(pool, tmp_path, tag="gate")
    elif seed == "regression":
        model_id = seed_regression(pool)
    else:
        model_id = seed_survival(pool, tmp_path)["model_id"]
    response = client.get(f"/api/models/{model_id}/{path}")
    assert response.status_code == 422, response.text
    assert named in response.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "residuals",
        "residuals/segments",
        "forecast-actual",
        "baseline",
        "structure",
        "gains",
        "survival",
        "near-miss",
        "prototypes",
    ],
)
def test_an_unknown_model_is_404(client, db_pool_greenfield, path):
    assert client.get(f"/api/models/987654/{path}").status_code == 404


# ------------------------------------------------------------------- regression


def test_the_residual_routes_answer_the_readers_rows(client, db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = seed_regression(pool)
    _same(
        client.get(f"/api/models/{model_id}/residuals").json(),
        readers.residual_summary(pool, model_id),
    )
    for cut in ("band", "sbc", "cold-start"):
        body = client.get(
            f"/api/models/{model_id}/residuals/segments",
            params={"cut": cut, "pooled": "true"},
        ).json()
        _same(body, readers.residuals_by_segment(pool, model_id, cut, pooled=True))
        assert body and all(r["as_of_date"] is None for r in body)
    both = client.get(f"/api/models/{model_id}/residuals/segments").json()
    assert {r["as_of_date"] is None for r in both} == {True, False}
    bad = client.get(f"/api/models/{model_id}/residuals/segments?cut=decile")
    assert bad.status_code == 422 and "subset:NAME" in bad.json()["detail"]
    missing = client.get(f"/api/models/{model_id}/residuals/segments?cut=subset:nope")
    assert missing.status_code == 404


def test_forecast_actual_is_capped(client, db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = seed_regression(pool)
    full = client.get(f"/api/models/{model_id}/forecast-actual").json()
    _same(full, readers.forecast_actual(pool, model_id, cap=20_000))
    assert full["sampled"] is False and len(full["pairs"]) == full["n_total"]
    capped = client.get(f"/api/models/{model_id}/forecast-actual?cap=5").json()
    assert capped["sampled"] is True and len(capped["pairs"]) == 5
    assert capped["n_total"] == full["n_total"]
    too_many = client.get(f"/api/models/{model_id}/forecast-actual?cap=10000000")
    assert too_many.status_code == 422


def test_an_unscored_model_answers_the_named_empty_states(client, db_pool_greenfield):
    model_id = _unscored_regression_model(db_pool_greenfield)
    for path, command in (
        ("residuals", "triage postmodel residuals"),
        ("residuals/segments", "triage postmodel residuals"),
        ("forecast-actual", "triage plot forecast-actual"),
        ("gains", "triage plot gains"),
    ):
        body = client.get(f"/api/models/{model_id}/{path}").json()
        assert body["empty"] is True, path
        assert command in body["hint"] and "triage run" in body["hint"], path
    structure = client.get(f"/api/models/{model_id}/structure").json()
    assert structure["empty"] is True and "artifact_uri" in structure["reason"]
    assert f"triage postmodel structure {model_id}" in structure["hint"]


def test_the_baseline_route_is_the_group_comparison(client, db_pool_greenfield):
    from triage.diagnostics.baseline_compare import compare_to_baseline

    pool = db_pool_greenfield
    groups = seed_baseline(pool)
    model_id = _model_of(pool, groups["dominant"])
    body = client.get(f"/api/models/{model_id}/baseline").json()
    _same(
        body,
        compare_to_baseline(pool, groups["dominant"], experiment_hash=BASELINE_EXP)
        .to_dict(),
    )  # fmt: skip
    # the seasonal baseline misses by 2, persistence by 5: the default is the closer one
    assert body["baseline_group_id"] == groups["seasonal"] and body["baseline_chosen"]
    assert body["segments"] and all(s["model_wins"] for s in body["segments"])
    named = client.get(
        f"/api/models/{model_id}/baseline",
        params={"baseline": groups["persistence"], "cut": "band"},
    ).json()
    assert named["baseline_group_id"] == groups["persistence"]
    assert {s["cut"] for s in named["segments"]} == {"band"}
    bad = client.get(f"/api/models/{model_id}/baseline?cut=decile")
    assert bad.status_code == 422 and "is not a cut" in bad.json()["detail"]


def test_an_experiment_without_a_baseline_group_is_the_empty_state(
    client, db_pool_greenfield
):
    model_id = seed_regression(db_pool_greenfield)  # one DummyRegressor group only
    body = client.get(f"/api/models/{model_id}/baseline").json()
    assert body["empty"] is True
    assert "triage postmodel baseline" in body["hint"] and "--baseline" in body["hint"]


def test_the_structure_route_reads_the_estimator(client, db_pool_greenfield, tmp_path):
    ridge, x_train, x_test = _ridge_design()
    model_id = store_estimator(db_pool_greenfield, tmp_path, ridge, x_train, x_test)
    body = client.get(f"/api/models/{model_id}/structure").json()
    assert body["estimator"] == "Ridge" and body["problem_type"] == "regression"
    assert body["mean_shift"]["check_error"] < 1e-6
    assert body["unavailable"] == ["leaf support not available for Ridge"]
    assert "leaf_support" not in body


def test_error_rules_read_the_regression_kinds(client, db_pool_greenfield):
    pool = db_pool_greenfield
    model_id = seed_regression(pool)
    empty = client.get(f"/api/models/{model_id}/error-rules").json()
    assert empty["empty"] is True and "--kind abs-error" in empty["hint"]
    beats = client.get(
        f"/api/models/{model_id}/error-rules?error_kind=beats_baseline"
    ).json()
    assert beats["empty"] is True and "--kind beats-baseline" in beats["hint"]
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.error_analysis (model_id, split_kind, as_of_date,"
            " parameter, error_kind, rule_id, rule, n_matched, n_errors, error_rate,"
            " depth, signature, leaf_stats) values (%(m)s, 'test', date '2015-02-01',"
            " '', 'abs_error', 0, 'f > 1', 40, 12, 2.5, 1, 'f>',"
            ' \'{"mean_abs_error": 5.0, "population_mae": 2.0}\'::jsonb)',
            {"m": model_id},
        )
    rows = client.get(f"/api/models/{model_id}/error-rules?error_kind=abs_error").json()
    assert [r["error_kind"] for r in rows] == ["abs_error"]
    assert rows[0]["reference_error_rate"] == 1.0
    assert rows[0]["leaf_stats"]["population_mae"] == 2.0


def _group_of(pool, model_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "select model_group_id from triage.models where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()["model_group_id"]


def test_the_group_sheet_names_its_problem_type(client, seeded, db_pool_greenfield):
    mg1 = seeded.group_ids["mg1"]
    assert client.get(f"/api/model-groups/{mg1}").json()["problem_type"] == (
        "classification"
    )
    regression = _group_of(db_pool_greenfield, seed_regression(db_pool_greenfield))
    body = client.get(f"/api/model-groups/{regression}").json()
    assert body["problem_type"] == "regression"
    # scoped to an Experiment the group has no model in, it has no type
    scoped = client.get(
        f"/api/model-groups/{regression}", params={"experiment_hash": "elsewhere"}
    ).json()
    assert scoped["problem_type"] is None


def test_a_regression_groups_recurrence_reads_its_leaves(
    client, db_pool_greenfield, tmp_path
):
    from triage.diagnostics import compute_error_analysis

    pool = db_pool_greenfield
    seeded = _seed_regression_trees(pool, tmp_path)
    gid = seeded["group"]
    route = f"/api/model-groups/{gid}/error-recurrence"
    before = client.get(route, params={"error_kind": "abs_error"}).json()
    assert before["empty"] is True and "--kind abs-error" in before["hint"]

    for kind in ("abs_error", "beats_baseline"):
        compute_error_analysis(
            pool, seeded["model"], kind=kind, max_depth=2, min_samples_leaf=20
        )
    abs_error = client.get(route, params={"error_kind": "abs_error"}).json()
    _same(abs_error, readers.error_recurrence(pool, gid, error_kind="abs_error"))
    assert abs_error["parameters"] == [""] and abs_error["parameter"] == ""
    worst = max(abs_error["rules"], key=lambda r: r["mean_error_rate"])
    assert worst["example_rule"].startswith("x1 > 5")
    assert worst["n_dates"] == worst["n_dates_total"] == len(REG_DATES)
    # without a kind, the route reads the first kind the group's type has
    assert client.get(route).json()["error_kind"] == "abs_error"

    beats = client.get(route, params={"error_kind": "beats_baseline"}).json()
    assert beats["parameters"] == [f"baseline:{seeded['baseline']}"]
    assert beats["rules"] and {r["n_dates_total"] for r in beats["rules"]} == {1}


@pytest.mark.parametrize(
    ("seed", "kind", "named"),
    [
        ("regression", "fp", "error-recurrence {g} --kind abs-error"),
        ("regression", "confusion", "error-recurrence {g} --kind abs-error"),
        ("classification", "abs_error", "error-recurrence {g} --kind fp"),
        ("survival", "beats_baseline", "--kind fp -p 'CUT|horizon=H'"),
    ],
)
def test_a_groups_recurrence_refuses_a_kind_its_type_lacks(
    client, db_pool_greenfield, tmp_path, seed, kind, named
):
    pool = db_pool_greenfield
    if seed == "classification":
        model_id = seed_near_miss(pool, tmp_path, tag="gate")
    elif seed == "regression":
        model_id = seed_regression(pool)
    else:
        model_id = seed_survival(pool, tmp_path)["model_id"]
    gid = _group_of(pool, model_id)
    response = client.get(
        f"/api/model-groups/{gid}/error-recurrence", params={"error_kind": kind}
    )
    assert response.status_code == 422, response.text
    assert named.format(g=gid) in response.json()["detail"]


def test_a_regression_ranking_groups_recurrence_reads_both_families(
    client, db_pool_greenfield, tmp_path
):
    pool = db_pool_greenfield
    gid = _seed_regression_trees(pool, tmp_path, problem_type="regression_ranking")[
        "group"
    ]
    route = f"/api/model-groups/{gid}/error-recurrence"
    for kind in ("fp", "fn", "confusion", "abs_error", "beats_baseline"):
        response = client.get(route, params={"error_kind": kind})
        assert response.status_code == 200, (kind, response.text)
        assert response.json()["empty"] is True


# ------------------------------------------------------------- regression_ranking


def test_the_gains_route_is_the_readers_curve(client, db_pool_greenfield):
    pool = db_pool_greenfield
    models = seed_allocation(pool)
    model_id = models["perfect"]
    body = client.get(
        f"/api/models/{model_id}/gains", params={"points": 10, "as_of_date": str(R1)}
    ).json()
    _same(body, readers.gains_curve(pool, model_id, as_of_date=R1, points=10))
    assert body["problem_type"] == "regression_ranking" and len(body["rows"]) == 11
    assert body["aucc"] is not None
    assert client.get(f"/api/models/{model_id}/gains?points=0").status_code == 422
    # the decile table with lift is the calibration route's (migration 0029)
    deciles = client.get(f"/api/models/{model_id}/calibration").json()["deciles"]
    assert deciles and all("lift" in d for d in deciles)


# ------------------------------------------------------------- classification


def test_near_miss_and_prototypes(client, db_pool_greenfield, tmp_path):
    from tests.test_near_miss_prototypes import D1
    from triage.diagnostics.prototypes import compute_prototypes

    pool = db_pool_greenfield
    model_id = seed_near_miss(pool, tmp_path, tag="routes")
    bands = client.get(f"/api/models/{model_id}/near-miss?parameter=100_abs").json()
    _same(bands, readers.near_miss(pool, model_id, "100_abs"))
    assert [b["band"] for b in bands[:3]] == ["selected", "near_miss", "rest"]
    one = client.get(
        f"/api/models/{model_id}/near-miss",
        params={"parameter": "100_abs", "as_of_date": str(D1)},
    ).json()
    assert {b["as_of_date"] for b in one} == {str(D1)}
    before = client.get(f"/api/models/{model_id}/prototypes").json()
    assert before["empty"] is True
    assert f"triage postmodel prototypes {model_id}" in before["hint"]
    compute_prototypes(pool, model_id, "100_abs", n=2)
    after = client.get(f"/api/models/{model_id}/prototypes?parameter=100_abs").json()
    _same(after, readers.prototypes(pool, model_id, parameter="100_abs"))
    assert {r["class"] for r in after} == {"tp", "fp", "fn", "tn"}


def test_crosstabs_against_the_near_miss(client, seeded, db_pool_greenfield):
    from tests.dashboard_tests.conftest import SPLITS

    model_id = seeded.all_models["mg1"][0]
    empty = client.get(f"/api/models/{model_id}/crosstabs?comparison=near_miss").json()
    assert empty["empty"] is True and "--against near-miss" in empty["hint"]
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "insert into triage.crosstabs (model_id, split_kind, as_of_date, parameter,"
            " comparison, feature, stat, selected_value, rest_value, ratio)"
            " values (%(m)s, 'test', %(d)s, '3_abs', 'near_miss', 'f1', 'mean', 2.0,"
            " 1.0, 2.0)",
            {"m": model_id, "d": SPLITS[-1]},
        )
    rows = client.get(f"/api/models/{model_id}/crosstabs?comparison=near_miss").json()
    assert [r["feature"] for r in rows] == ["f1"]
    assert client.get(f"/api/models/{model_id}/crosstabs").json()["empty"] is True
    bad = client.get(f"/api/models/{model_id}/crosstabs?comparison=random")
    assert bad.status_code == 422


# ------------------------------------------------------------------- survival


def test_the_survival_route_reads_the_persisted_set(
    client, db_pool_greenfield, tmp_path
):
    from triage.diagnostics.survival import compute_survival_diagnostics

    pool = db_pool_greenfield
    model_id = seed_survival(pool, tmp_path)["model_id"]
    before = client.get(f"/api/models/{model_id}/survival").json()
    assert before["empty"] is True
    assert f"triage postmodel survival {model_id} --horizon H" in before["hint"]
    compute_survival_diagnostics(pool, model_id, H, TIMES)
    body = client.get(f"/api/models/{model_id}/survival").json()
    _same(body, readers.survival_diagnostics(pool, model_id))
    assert body["horizon"] == H and body["kinds"]["km_decile"]
    elsewhere = client.get(f"/api/models/{model_id}/survival?horizon=999").json()
    assert elsewhere["empty"] is True and "999" in elsewhere["reason"]
    # the histogram splits a survival model's rows into events and censored rows
    bins = client.get(f"/api/models/{model_id}/histogram").json()
    assert sum(b["n_event"] + b["n_censored"] for b in bins) == sum(
        b["n"] for b in bins
    )
    hint = client.get(f"/api/models/{model_id}/error-rules").json()["hint"]
    assert "--horizon H" in hint


# ------------------------------------------------------------------- common rows


def test_the_common_rows_read(client, db_pool_greenfield):
    pool = db_pool_greenfield
    groups = seed_common_rows(pool)
    body = client.get(f"/api/experiments/{COMMON_EXP}/common-rows").json()
    _same(body, readers.common_rows(pool, groups.values()))
    two = client.get(
        f"/api/experiments/{COMMON_EXP}/common-rows",
        params={"groups": [groups["A"], groups["C"]]},
    ).json()
    assert {r["model_group_id"] for r in two} == {groups["A"], groups["C"]}
    one = client.get(
        f"/api/experiments/{COMMON_EXP}/common-rows", params={"groups": groups["A"]}
    )
    assert one.status_code == 422 and "two or more" in one.json()["detail"]
    assert client.get("/api/experiments/nope/common-rows").status_code == 404


@pytest.fixture
def write_client(db_url, db_pool_greenfield, monkeypatch):
    """The app with a registry over the same test DB, the 'food' project routed to it."""
    monkeypatch.setenv("TRIAGE_PROJECT_DB_MAP", json.dumps({"food": db_url}))
    upgrade_registry_db(db_url)
    auth = TrustedHeaderAuth(
        default_user="admin@test", admin_emails=frozenset({"admin@test"})
    )
    app = create_app(
        pool=db_pool_greenfield, registry_pool=db_pool_greenfield, auth_backend=auth
    )
    with TestClient(app) as c:
        yield c


def test_materializing_common_rows_changes_the_audition_pick(
    write_client, db_pool_greenfield
):
    client = write_client
    groups = seed_common_rows(db_pool_greenfield)
    own = client.get(f"/api/experiments/{COMMON_EXP}/audition?metric=mae").json()
    assert own["pick"] == groups["C"]  # it skipped the hard rows
    assert own["subset_hash"] == ""
    made = client.post(f"/api/experiments/{COMMON_EXP}/common-rows", json={})
    assert made.status_code == 200, made.text
    common = made.json()
    assert common["subset_hash"].startswith("common:")
    assert (common["n_rows"], common["n_dates"]) == (160, 2)
    assert common["model_group_ids"] == sorted(groups.values())
    on_common = client.get(
        f"/api/experiments/{COMMON_EXP}/audition",
        params={"metric": "mae", "subset_hash": common["subset_hash"]},
    ).json()
    assert on_common["pick"] == groups["A"]
    assert {r["model_group_id"] for r in on_common["ranking"]} == set(groups.values())
    assert all(c["dist_from_best_case"] is not None for c in on_common["curves"])
    # the full-cohort audition is untouched
    again = client.get(f"/api/experiments/{COMMON_EXP}/audition?metric=mae").json()
    assert again["pick"] == groups["C"]


def test_materializing_common_rows_is_a_write(write_client, db_pool_greenfield):
    client = write_client
    seed_common_rows(db_pool_greenfield)
    created = client.post(
        "/api/projects", json={"slug": "food", "display_name": "Food"}
    )
    assert created.status_code == 201, created.text
    outsider = client.post(
        f"/api/experiments/{COMMON_EXP}/common-rows",
        json={},
        headers={"X-Triage-User": "someone@else", "X-Triage-Project": "food"},
    )
    assert outsider.status_code == 403
    one = client.post(
        f"/api/experiments/{COMMON_EXP}/common-rows", json={"model_group_ids": [1]}
    )
    assert one.status_code == 422
    unknown = client.post("/api/experiments/nope/common-rows", json={})
    assert unknown.status_code == 404


def test_materializing_common_rows_needs_a_registry(db_pool_greenfield, monkeypatch):
    monkeypatch.delenv("TRIAGE_REGISTRY_URL", raising=False)
    seed_common_rows(db_pool_greenfield)
    with TestClient(create_app(pool=db_pool_greenfield)) as client:
        response = client.post(f"/api/experiments/{COMMON_EXP}/common-rows", json={})
    assert response.status_code == 503
    assert "registry" in response.json()["detail"]
