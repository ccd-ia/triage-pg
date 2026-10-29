"""Dashboard reads of the error trees (migration 0027): rules by kind, suspicious leaves, recurrence.

The routes are thin reads over ``triage.error_analysis`` and the two 0027 functions, so these
tests hand-insert leaves on the shared dashboard fixture and check what each route passes
through: the kind filter, the suspicious thresholds, the cut a recurrence is read at, and the
empty states that tell the user which CLI command fills the panel.
"""

from __future__ import annotations

import json

import pytest

from tests.dashboard_tests.conftest import SPLITS

PARAMETER = "2_abs"


def _leaf(
    pool,
    model_id: int,
    as_of: str,
    kind: str,
    rule_id: int,
    signature: str,
    n_matched: int,
    n_errors: int,
    class_counts: dict[str, int],
    parameter: str = PARAMETER,
) -> None:
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.error_analysis (model_id, split_kind, as_of_date,"
            " parameter, error_kind, rule_id, rule, n_matched, n_errors, error_rate,"
            " depth, class_counts, signature)"
            " values (%(m)s, 'test', %(d)s, %(p)s, %(k)s, %(r)s, %(rule)s, %(n)s, %(e)s,"
            "         %(er)s, 1, cast(%(cc)s as jsonb), %(sig)s)",
            {
                "m": model_id,
                "d": as_of,
                "p": parameter,
                "k": kind,
                "r": rule_id,
                "rule": f"rule {signature}",
                "n": n_matched,
                "e": n_errors,
                "er": n_errors / n_matched,
                "cc": json.dumps(class_counts),
                "sig": signature,
            },
        )


@pytest.fixture
def analysed(seeded, db_pool_greenfield):
    """mg1's three models, each with an fp, fn and confusion tree at ``PARAMETER``.

    The fp leaf ``a>`` is on every date with 100 rows and one error; the latest model also
    has precision@2_abs = 0.5, so its ``a>`` leaf has lift (1 - 1/100) / 0.5.
    """
    pool = db_pool_greenfield
    for model_id, as_of in zip(seeded.all_models["mg1"], SPLITS):
        _leaf(pool, model_id, as_of, "fp", 0, "a>", 100, 1, {"tp": 99, "fp": 1})
        _leaf(pool, model_id, as_of, "fp", 1, "a<=", 40, 30, {"tp": 10, "fp": 30})
        _leaf(pool, model_id, as_of, "fn", 0, "b>", 50, 20, {"fn": 20, "tn": 30})
        _leaf(
            pool,
            model_id,
            as_of,
            "confusion",
            0,
            "a>|b<=",
            60,
            30,
            {"tp": 10, "fp": 20, "fn": 10, "tn": 20},
        )
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.evaluations (model_id, split_kind, as_of_date, metric,"
            " parameter, value) values (%(m)s, 'test', %(d)s, 'precision@', %(p)s, 0.5)",
            {"m": seeded.all_models["mg1"][-1], "d": SPLITS[-1], "p": PARAMETER},
        )
    return seeded


def test_error_rules_empty_state_names_the_command(client, seeded):
    resp = client.get(f"/api/models/{seeded.all_models['mg2'][0]}/error-rules")
    assert resp.status_code == 200
    body = resp.json()
    assert body["empty"] is True
    assert "error-tree" in body["hint"] and "--kind all" in body["hint"]


def test_error_rules_carry_class_counts_and_filter_by_kind(client, analysed):
    model_id = analysed.all_models["mg1"][-1]
    rows = client.get(f"/api/models/{model_id}/error-rules").json()
    assert {r["error_kind"] for r in rows} == {"fp", "fn", "confusion"}
    assert all({"class_counts", "signature"} <= set(r) for r in rows)

    confusion = client.get(
        f"/api/models/{model_id}/error-rules", params={"error_kind": "confusion"}
    ).json()
    assert len(confusion) == 1
    assert confusion[0]["class_counts"] == {"tp": 10, "fp": 20, "fn": 10, "tn": 20}
    assert confusion[0]["signature"] == "a>|b<="

    bogus = client.get(
        f"/api/models/{model_id}/error-rules", params={"error_kind": "tp"}
    )
    assert bogus.status_code == 422


def test_error_rules_suspicious_reads_the_0027_function(client, analysed):
    model_id = analysed.all_models["mg1"][-1]
    rows = client.get(
        f"/api/models/{model_id}/error-rules", params={"suspicious": "true"}
    ).json()
    assert [(r["error_kind"], r["signature"]) for r in rows] == [("fp", "a>")]
    row = rows[0]
    assert row["tp_share"] == pytest.approx(99 / 100)
    assert row["precision_at_k"] == pytest.approx(0.5)
    assert row["lift"] == pytest.approx((1 - 1 / 100) / 0.5)

    # an earlier model has no precision@ row: the leaf qualifies, its lift is unknown
    earlier = client.get(
        f"/api/models/{analysed.all_models['mg1'][0]}/error-rules",
        params={"suspicious": "true"},
    ).json()
    assert len(earlier) == 1 and earlier[0]["lift"] is None

    # the thresholds pass through; with none qualifying the answer is an empty list
    none = client.get(
        f"/api/models/{model_id}/error-rules",
        params={"suspicious": "true", "min_support": 101},
    ).json()
    assert none == []
    out_of_range = client.get(
        f"/api/models/{model_id}/error-rules",
        params={"suspicious": "true", "max_error_rate": 2},
    )
    assert out_of_range.status_code == 422


def test_error_recurrence_counts_dates_across_the_group(
    client, analysed, db_pool_greenfield
):
    gid = analysed.group_ids["mg1"]
    body = client.get(f"/api/model-groups/{gid}/error-recurrence").json()
    assert body["parameter"] == PARAMETER and body["error_kind"] == "fp"
    assert body["parameters"] == [PARAMETER]
    by_sig = {r["signature"]: r for r in body["rules"]}
    assert set(by_sig) == {"a>", "a<="}
    assert (by_sig["a>"]["n_dates"], by_sig["a>"]["n_dates_total"]) == (3, 3)

    confusion = client.get(
        f"/api/model-groups/{gid}/error-recurrence", params={"error_kind": "confusion"}
    ).json()
    assert [r["signature"] for r in confusion["rules"]] == ["a>|b<="]

    # a second cut, analysed later, becomes the default; the first stays selectable
    model_id = analysed.all_models["mg1"][0]
    _leaf(
        db_pool_greenfield,
        model_id,
        SPLITS[0],
        "fp",
        0,
        "c>",
        30,
        3,
        {"tp": 27, "fp": 3},
        parameter="10_pct",
    )
    latest = client.get(f"/api/model-groups/{gid}/error-recurrence").json()
    assert latest["parameter"] == "10_pct"
    assert latest["parameters"] == ["10_pct", PARAMETER]
    assert [r["signature"] for r in latest["rules"]] == ["c>"]
    pinned = client.get(
        f"/api/model-groups/{gid}/error-recurrence", params={"parameter": PARAMETER}
    ).json()
    assert {r["signature"] for r in pinned["rules"]} == {"a>", "a<="}


def test_error_recurrence_empty_and_missing_groups(client, seeded):
    empty = client.get(
        f"/api/model-groups/{seeded.group_ids['mg2']}/error-recurrence"
    ).json()
    assert empty["empty"] is True and "error-tree" in empty["hint"]
    assert client.get("/api/model-groups/999999/error-recurrence").status_code == 404
    bogus = client.get(
        f"/api/model-groups/{seeded.group_ids['mg2']}/error-recurrence",
        params={"error_kind": "both"},
    )
    assert bogus.status_code == 422
