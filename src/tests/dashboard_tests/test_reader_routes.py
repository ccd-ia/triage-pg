"""What the routes gained from the shared query layer: ``?points=`` on the curve, ``?stat=`` on
the crosstabs, ``reference_error_rate`` on the error rules, and the group's curves across test
dates (``/model-groups/{id}/curves``).

The seeded experiment scores one model — mg1's first — on four entities at the last split:
scores 0.9 / 0.7 / 0.4 / 0.1, outcomes 1 / 0 / 1 / 0. At the cut 3_abs that is tp 2, fp 1,
fn 0, tn 1, so the population error rates are fp 1/3, fn 0 and confusion 1/4.
"""

from __future__ import annotations

import json

import pytest

from tests.dashboard_tests.conftest import SPLITS


def _leaf(pool, model_id: int, as_of: str, kind: str, n: int, errors: int) -> None:
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.error_analysis (model_id, split_kind, as_of_date,"
            " parameter, error_kind, rule_id, rule, n_matched, n_errors, error_rate,"
            " depth, class_counts, signature)"
            " values (%(m)s, 'test', %(d)s, '3_abs', %(k)s, 0, 'f1 > 1', %(n)s, %(e)s,"
            "         %(er)s, 1, cast(%(cc)s as jsonb), 'f1>')",
            {
                "m": model_id,
                "d": as_of,
                "k": kind,
                "n": n,
                "e": errors,
                "er": errors / n,
                "cc": json.dumps({"tp": n - errors, "fp": errors}),
            },
        )


def _crosstab(pool, model_id: int, feature: str, stat: str, s: float, r: float):
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.crosstabs (model_id, split_kind, as_of_date, parameter,"
            " feature, stat, selected_value, rest_value, ratio)"
            " values (%(m)s, 'test', %(d)s, '3_abs', %(f)s, %(stat)s, %(s)s, %(r)s,"
            "         %(s)s / %(r)s)",
            {
                "m": model_id,
                "d": SPLITS[-1],
                "f": feature,
                "stat": stat,
                "s": s,
                "r": r,
            },
        )


# ---------------------------------------------------------------------------- curve


def test_curve_points_thins_to_evenly_spaced_shares(client, seeded):
    model_id = seeded.all_models["mg1"][0]
    full = client.get(f"/api/models/{model_id}/curve").json()
    thin = client.get(f"/api/models/{model_id}/curve", params={"points": 2}).json()
    # shares 0 and 1 of four rows: the first and the last
    assert [r["k"] for r in thin] == [1, 4]
    assert thin == [full[0], full[-1]]
    too_few = client.get(f"/api/models/{model_id}/curve", params={"points": 1})
    assert too_few.status_code == 422


# -------------------------------------------------------------------- group curves


def test_group_curves_one_series_per_model_and_date(client, seeded):
    gid = seeded.group_ids["mg1"]
    model_id = seeded.all_models["mg1"][0]
    full = client.get(f"/api/models/{model_id}/curve").json()

    series = client.get(f"/api/model-groups/{gid}/curves").json()
    assert [(s["model_id"], s["as_of_date"]) for s in series] == [
        (model_id, SPLITS[-1])
    ]
    assert series[0]["base_rate"] == pytest.approx(0.5)
    assert series[0]["population"] == 4
    assert series[0]["points"] == full  # 101 shares of four rows keep every row

    thin = client.get(f"/api/model-groups/{gid}/curves", params={"points": 2}).json()
    assert [r["k"] for r in thin[0]["points"]] == [1, 4]
    scoped = client.get(
        f"/api/model-groups/{gid}/curves",
        params={"experiment_hash": seeded.experiment_hash},
    ).json()
    assert scoped == series


def test_group_curves_empty_missing_and_bad_input(client, seeded):
    gid = seeded.group_ids["mg1"]
    unscored = client.get(f"/api/model-groups/{seeded.group_ids['mg2']}/curves").json()
    assert unscored["empty"] is True and "score" in unscored["hint"]
    foreign = client.get(
        f"/api/model-groups/{gid}/curves", params={"experiment_hash": "does-not-exist"}
    ).json()
    assert foreign["empty"] is True

    missing = client.get("/api/model-groups/999999/curves")
    assert missing.status_code == 404
    assert "model group 999999" in missing.json()["detail"]
    no_subset = client.get(f"/api/model-groups/{gid}/curves", params={"subset": "nope"})
    assert no_subset.status_code == 404 and "subset" in no_subset.json()["detail"]
    too_few = client.get(f"/api/model-groups/{gid}/curves", params={"points": 1})
    assert too_few.status_code == 422


def test_the_overlay_calls_draw_the_subsets_own_curve(
    client, seeded, db_pool_greenfield
):
    """The panels' two subset calls: ``/curve?subset=&points=`` (one line per population on
    the model sheet) and ``/curves?subset=`` (the group sheet's Subset selector). Both draw
    the subset as its own population, as the full single-model curve does."""
    model_id = seeded.all_models["mg1"][0]
    gid = seeded.group_ids["mg1"]
    with db_pool_greenfield.connection() as conn:
        conn.execute(
            "insert into triage.subsets (subset_hash, config)"
            " values ('sub-overlay', '{\"name\": \"first three\"}'::jsonb)"
        )
        # scores 0.9 / 0.7 / 0.4, outcomes 1 / 0 / 1
        for entity_id in (1, 2, 3):
            conn.execute(
                "insert into triage.subset_members (subset_hash, entity_id, as_of_date)"
                " values ('sub-overlay', %(e)s, %(d)s)",
                {"e": entity_id, "d": SPLITS[-1]},
            )
    curve = f"/api/models/{model_id}/curve"
    full = client.get(curve, params={"subset": "sub-overlay"}).json()
    assert [r["k"] for r in full] == [1, 2, 3]

    thin = client.get(curve, params={"subset": "sub-overlay", "points": 2}).json()
    assert thin == [full[0], full[-1]]  # shares 0 and 1 of the subset's three rows

    series = client.get(
        f"/api/model-groups/{gid}/curves", params={"subset": "sub-overlay"}
    ).json()
    assert [(s["model_id"], s["population"]) for s in series] == [(model_id, 3)]
    assert series[0]["base_rate"] == pytest.approx(2 / 3)
    assert series[0]["points"] == full


# ------------------------------------------------------------------------ crosstabs


def test_crosstabs_stat_ranks_nonzero_rates_by_difference(
    client, seeded, db_pool_greenfield
):
    model_id = seeded.all_models["mg1"][0]
    pool = db_pool_greenfield
    for feature, s, r in (("broad", 0.9, 0.3), ("tiny", 0.02, 0.001)):
        _crosstab(pool, model_id, feature, "nonzero_rate", s, r)
    for feature, s, r in (("double", 2.0, 1.0), ("quarter", 1.0, 4.0)):
        _crosstab(pool, model_id, feature, "mean", s, r)

    default = client.get(f"/api/models/{model_id}/crosstabs").json()
    assert [r["feature"] for r in default] == ["quarter", "double"]
    nonzero = client.get(
        f"/api/models/{model_id}/crosstabs", params={"stat": "nonzero_rate"}
    ).json()
    # |0.9 − 0.3| beats |0.02 − 0.001|, though the second ratio is the larger
    assert [r["feature"] for r in nonzero] == ["broad", "tiny"]
    assert set(nonzero[0]) == set(default[0])

    other = seeded.all_models["mg2"][0]
    empty = client.get(
        f"/api/models/{other}/crosstabs", params={"stat": "nonzero_rate"}
    ).json()
    assert empty["empty"] is True
    bogus = client.get(f"/api/models/{model_id}/crosstabs", params={"stat": "std"})
    assert bogus.status_code == 422


# ---------------------------------------------------------------------- error rules


def test_error_rules_carry_the_population_reference_rate(
    client, seeded, db_pool_greenfield
):
    model_id = seeded.all_models["mg1"][0]
    pool = db_pool_greenfield
    for kind in ("fp", "fn", "confusion"):
        _leaf(pool, model_id, SPLITS[-1], kind, 100, 1)
    _leaf(pool, model_id, SPLITS[0], "fp", 40, 10)  # a date the model never scored

    rows = client.get(f"/api/models/{model_id}/error-rules").json()
    reference = {
        (r["as_of_date"], r["error_kind"]): r["reference_error_rate"] for r in rows
    }
    assert reference[(SPLITS[-1], "fp")] == pytest.approx(1 / 3)
    assert reference[(SPLITS[-1], "fn")] == pytest.approx(0.0)
    assert reference[(SPLITS[-1], "confusion")] == pytest.approx(1 / 4)
    assert reference[(SPLITS[0], "fp")] is None

    suspicious = client.get(
        f"/api/models/{model_id}/error-rules", params={"suspicious": "true"}
    ).json()
    by_kind = {r["error_kind"]: r["reference_error_rate"] for r in suspicious}
    assert by_kind == {"fp": pytest.approx(1 / 3), "confusion": pytest.approx(1 / 4)}
