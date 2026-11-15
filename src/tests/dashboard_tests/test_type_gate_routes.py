"""The model and group routes answer 422 for a problem type their diagnostic lacks.

The threshold curve, the group's curves across dates, calibration, the crosstab against
the near miss and the error rules of one kind each read what only some problem types
define. A route asked about another type answers 422 with the CLI's refusal, which names
the command that does apply; the right type still answers its rows.
"""

from __future__ import annotations

from tests.test_diagnostics import _seed_regression_trees
from tests.test_regression_diagnostics import TEST_DATES
from tests.test_regression_diagnostics import _seed as seed_typed
from tests.test_survival_diagnostics import seed_survival


def _group_of(pool, model_id: int) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "select model_group_id from triage.models where model_id = %(m)s",
            {"m": model_id},
        ).fetchone()["model_group_id"]


def _refused(response, *fragments: str) -> None:
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    for fragment in fragments:
        assert fragment in detail, detail


def test_the_curve_routes_refuse_a_model_without_a_cut(client, db_pool_greenfield):
    pool = db_pool_greenfield
    regression = seed_typed(pool, "regression", "exp-route-reg")
    survival = seed_typed(pool, "survival", "exp-route-surv")
    _refused(
        client.get(f"/api/models/{regression}/curve"),
        "triage plot curve reads classification and regression_ranking models;"
        f" model {regression} is regression.",
        f"triage plot gains {regression}",
    )
    _refused(client.get(f"/api/models/{survival}/curve"), f"triage plot km {survival}")
    _refused(
        client.get(f"/api/model-groups/{_group_of(pool, regression)}/curves"),
        "triage plot curve-dates reads classification and regression_ranking model"
        " groups",
    )
    _refused(
        client.get(f"/api/model-groups/{_group_of(pool, survival)}/curves"),
        "triage plot km MODEL",
    )
    ranking = seed_typed(pool, "regression_ranking", "exp-route-rr")
    day = {"as_of_date": str(TEST_DATES[0])}
    assert client.get(f"/api/models/{ranking}/curve", params=day).json()
    series = client.get(f"/api/model-groups/{_group_of(pool, ranking)}/curves").json()
    assert [s["model_id"] for s in series] == [ranking] * len(TEST_DATES)
    assert client.get("/api/models/999999/curve").status_code == 404


def test_calibration_refuses_a_survival_model(client, db_pool_greenfield, tmp_path):
    m = seed_survival(db_pool_greenfield, tmp_path)["model_id"]
    _refused(
        client.get(f"/api/models/{m}/calibration"),
        f"model {m} is survival.",
        f"triage plot survival-calibration {m}",
    )
    regression = seed_typed(db_pool_greenfield, "regression", "exp-route-cal")
    body = client.get(f"/api/models/{regression}/calibration").json()
    assert body["empty"] is True  # read, though never evaluated


def test_the_near_miss_crosstab_refuses_a_model_without_a_cut(
    client, db_pool_greenfield
):
    m = seed_typed(db_pool_greenfield, "regression", "exp-route-nm")
    _refused(
        client.get(f"/api/models/{m}/crosstabs", params={"comparison": "near_miss"}),
        "triage postmodel crosstabs --against near-miss reads classification and"
        f" regression_ranking models; model {m} is regression.",
    )
    rest = client.get(f"/api/models/{m}/crosstabs").json()
    assert rest["empty"] is True and "--by abs-error" in rest["hint"]


def test_error_rules_refuse_a_kind_the_models_type_lacks(
    client, db_pool_greenfield, tmp_path
):
    pool = db_pool_greenfield
    m = _seed_regression_trees(pool, tmp_path)["model"]
    for suspicious in ("false", "true"):
        _refused(
            client.get(
                f"/api/models/{m}/error-rules",
                params={"error_kind": "fp", "suspicious": suspicious},
            ),
            f"model {m} is regression: its error trees have abs-error, beats-baseline"
            " leaves, not fp.",
        )
    empty = client.get(
        f"/api/models/{m}/error-rules", params={"error_kind": "abs_error"}
    )
    assert empty.status_code == 200 and empty.json()["empty"] is True
    # without a kind, every persisted kind is read
    assert client.get(f"/api/models/{m}/error-rules").json()["empty"] is True
    survival = seed_survival(pool, tmp_path)["model_id"]
    _refused(
        client.get(
            f"/api/models/{survival}/error-rules", params={"error_kind": "abs_error"}
        ),
        f"model {survival} is survival: its error trees have fp, fn, confusion",
    )
