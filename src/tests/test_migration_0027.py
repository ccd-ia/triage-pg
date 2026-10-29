"""Migration 0027 — the confusion error kind, structured leaf columns, two read functions.

Two things are tested here, on hand-inserted ``triage.error_analysis`` rows so every expected
number is written down in the test. First, the revision is a real round trip: downgrade removes
what upgrade added, and refuses to run while ``confusion`` rows exist, because the restored 0017
check would reject them. Second, the two SQL functions select and aggregate the rows they
claim to: ``error_rules_suspicious`` keeps high-support, near-zero-error fp leaves and
TP-dominated confusion leaves, and ``error_rule_recurrence`` counts the dates a signature is on.
"""

from __future__ import annotations

import json
from datetime import date

import psycopg
import pytest
import sqlalchemy.exc

from triage.component import results_schema

REVISION = "0027_error_confusion_recurrence"
PREVIOUS = "0026_scored_labels"
DATES = [date(2014, 1, 1), date(2014, 7, 1), date(2015, 1, 1)]
PARAMETER = "50_abs"
PRECISION = 0.40  # the model's precision@50_abs on every date


def _columns(engine) -> set[str]:
    with engine.connection() as conn:
        rows = conn.execute(
            "select column_name from information_schema.columns"
            " where table_schema = 'triage' and table_name = 'error_analysis'"
        ).fetchall()
    return {r["column_name"] for r in rows}


def _functions(engine) -> set[str]:
    with engine.connection() as conn:
        rows = conn.execute(
            "select p.proname from pg_proc p"
            " join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'triage'"
            "   and p.proname in ('error_rules_suspicious', 'error_rule_recurrence')"
        ).fetchall()
    return {r["proname"] for r in rows}


def _seed_group(engine) -> tuple[int, list[int]]:
    """One model group with one model per test date, each evaluated at ``PARAMETER``."""
    with engine.connection() as conn:
        group_id = conn.execute(
            "insert into triage.model_groups (model_group_hash, model_type,"
            " hyperparameters, feature_list) values ('mg-0027', 'x.Y', '{}'::jsonb,"
            " ARRAY['a', 'b']) returning model_group_id"
        ).fetchone()["model_group_id"]
        model_ids = []
        for i, as_of in enumerate(DATES):
            conn.execute(
                "insert into triage.artifacts (artifact_id, logical_id, kind, config)"
                " values (%(a)s, %(a)s, 'model', '{}'::jsonb)",
                {"a": f"m-0027-{i}"},
            )
            model_id = conn.execute(
                "insert into triage.models (model_group_id, model_hash, train_end_time)"
                " values (%(g)s, %(h)s, %(t)s) returning model_id",
                {"g": group_id, "h": f"m-0027-{i}", "t": as_of},
            ).fetchone()["model_id"]
            model_ids.append(model_id)
            conn.execute(
                "insert into triage.evaluations (model_id, split_kind, as_of_date,"
                " metric, parameter, value) values (%(m)s, 'test', %(d)s, 'precision@',"
                " %(p)s, %(v)s)",
                {"m": model_id, "d": as_of, "p": PARAMETER, "v": PRECISION},
            )
    return group_id, model_ids


def _leaf(
    engine,
    model_id: int,
    as_of: date,
    kind: str,
    rule_id: int,
    signature: str | None,
    n_matched: int,
    n_errors: int,
    class_counts: dict[str, int] | None,
) -> None:
    with engine.connection() as conn:
        conn.execute(
            "insert into triage.error_analysis (model_id, split_kind, as_of_date,"
            " parameter, error_kind, rule_id, rule, n_matched, n_errors, error_rate,"
            " depth, class_counts, signature)"
            " values (%(m)s, 'test', %(d)s, %(p)s, %(k)s, %(r)s, %(rule)s, %(n)s, %(e)s,"
            "         %(er)s, 1, cast(%(cc)s as jsonb), %(sig)s)",
            {
                "m": model_id,
                "d": as_of,
                "p": PARAMETER,
                "k": kind,
                "r": rule_id,
                "rule": f"{signature} @ {as_of} #{rule_id}",
                "n": n_matched,
                "e": n_errors,
                "er": n_errors / n_matched,
                "cc": None if class_counts is None else json.dumps(class_counts),
                "sig": signature,
            },
        )


def test_0027_round_trips(db_url, db_pool_greenfield) -> None:
    """downgrade removes the columns, the functions and the third kind; upgrade restores them."""
    engine = db_pool_greenfield
    added = {"class_counts", "conditions", "signature"}
    assert added <= _columns(engine), "the fixture migrates to head"
    assert _functions(engine) == {"error_rules_suspicious", "error_rule_recurrence"}

    results_schema.downgrade_db(dburl=db_url, revision=PREVIOUS)
    assert not added & _columns(engine), "downgrade must drop the three columns"
    assert _functions(engine) == set(), "downgrade must drop both functions"
    _, model_ids = _seed_group(engine)
    with (
        pytest.raises(psycopg.errors.CheckViolation),
        engine.connection() as conn,
    ):
        # the 0017 row shape: the restored check admits fp and fn only
        conn.execute(
            "insert into triage.error_analysis (model_id, split_kind, as_of_date,"
            " parameter, error_kind, rule_id, rule, n_matched, n_errors, error_rate,"
            " depth) values (%(m)s, 'test', %(d)s, %(p)s, 'confusion', 0, 'x', 10, 1,"
            " 0.1, 1)",
            {"m": model_ids[0], "d": DATES[0], "p": PARAMETER},
        )

    results_schema.upgrade_db(dburl=db_url, revision=REVISION)
    assert added <= _columns(engine)
    assert _functions(engine) == {"error_rules_suspicious", "error_rule_recurrence"}
    _leaf(engine, model_ids[0], DATES[0], "confusion", 0, "a<=", 10, 1, None)


def test_0027_check_admits_exactly_three_kinds(db_pool_greenfield) -> None:
    engine = db_pool_greenfield
    _, model_ids = _seed_group(engine)
    for i, kind in enumerate(("fp", "fn", "confusion")):
        _leaf(engine, model_ids[0], DATES[0], kind, i, "a<=", 10, 1, None)
    with pytest.raises(psycopg.errors.CheckViolation):
        _leaf(engine, model_ids[0], DATES[0], "tp", 9, "a<=", 10, 1, None)


def test_0027_downgrade_refuses_while_confusion_rows_exist(
    db_url, db_pool_greenfield
) -> None:
    """The restored 0017 check would reject them, so the downgrade stops and says what to do."""
    engine = db_pool_greenfield
    _, model_ids = _seed_group(engine)
    _leaf(engine, model_ids[0], DATES[0], "confusion", 0, "a<=", 10, 1, None)

    with pytest.raises(
        sqlalchemy.exc.DBAPIError, match="delete error_kind = 'confusion' rows first"
    ):
        results_schema.downgrade_db(dburl=db_url, revision=PREVIOUS)
    assert "signature" in _columns(engine), "a refused downgrade must change nothing"

    with engine.connection() as conn:
        conn.execute("delete from triage.error_analysis where error_kind = 'confusion'")
    results_schema.downgrade_db(dburl=db_url, revision=PREVIOUS)
    assert "signature" not in _columns(engine)
    results_schema.upgrade_db(dburl=db_url, revision=REVISION)


def test_error_rules_suspicious_selects_and_computes_lift(db_pool_greenfield) -> None:
    engine = db_pool_greenfield
    _, model_ids = _seed_group(engine)
    m, d = model_ids[0], DATES[0]
    # fp leaves: (rule_id, n_matched, n_errors) — only rule 0 is large AND nearly error-free
    _leaf(engine, m, d, "fp", 0, "a>", 100, 1, {"tp": 99, "fp": 1})  # 1% error: kept
    _leaf(engine, m, d, "fp", 1, "a<=", 10, 0, {"tp": 10, "fp": 0})  # support 10 < 30
    _leaf(engine, m, d, "fp", 2, "b>", 100, 30, {"tp": 70, "fp": 30})  # 30% error
    # fn leaves are never "suspiciously right": a zero error rate there means no misses
    _leaf(engine, m, d, "fn", 0, "a<=", 200, 0, {"fn": 0, "tn": 200})
    # confusion leaves qualify on their TP share, not on their error rate
    tp_heavy = {"tp": 99, "fp": 0, "fn": 1, "tn": 0}  # 99% TP
    tn_heavy = {"tp": 0, "fp": 0, "fn": 1, "tn": 99}  # 1% error, but no TP at all
    _leaf(engine, m, d, "confusion", 0, "a>|b>", 100, 1, tp_heavy)
    _leaf(engine, m, d, "confusion", 1, "a<=", 100, 1, tn_heavy)

    with engine.connection() as conn:
        rows = conn.execute(
            "select * from triage.error_rules_suspicious(%(m)s)", {"m": m}
        ).fetchall()
    kept = {(r["error_kind"], r["rule_id"]) for r in rows}
    assert kept == {("fp", 0), ("confusion", 0)}

    by_kind = {r["error_kind"]: r for r in rows}
    assert by_kind["fp"]["precision_at_k"] == pytest.approx(PRECISION)
    assert by_kind["fp"]["lift"] == pytest.approx((1 - 0.01) / PRECISION, abs=1e-12)
    assert by_kind["fp"]["tp_share"] == pytest.approx(0.99, abs=1e-12)
    assert by_kind["confusion"]["tp_share"] == pytest.approx(0.99, abs=1e-12)
    assert by_kind["confusion"]["lift"] == pytest.approx((1 - 0.01) / PRECISION)
    assert rows[0]["lift"] >= rows[-1]["lift"], "ordered by lift, highest first"

    # the thresholds are parameters: loosen them and the 30%-error, 10-row leaves come in
    with engine.connection() as conn:
        loose = conn.execute(
            "select error_kind, rule_id from triage.error_rules_suspicious(%(m)s, 0.5, 5)",
            {"m": m},
        ).fetchall()
    assert {("fp", 1), ("fp", 2)} <= {(r["error_kind"], r["rule_id"]) for r in loose}


def test_error_rules_suspicious_lift_is_null_without_an_evaluation(
    db_pool_greenfield,
) -> None:
    engine = db_pool_greenfield
    _, model_ids = _seed_group(engine)
    with engine.connection() as conn:
        conn.execute("delete from triage.evaluations")
    _leaf(engine, model_ids[0], DATES[0], "fp", 0, "a>", 100, 0, {"tp": 100, "fp": 0})
    with engine.connection() as conn:
        row = conn.execute(
            "select precision_at_k, lift from triage.error_rules_suspicious(%(m)s)",
            {"m": model_ids[0]},
        ).fetchone()
    assert row["precision_at_k"] is None and row["lift"] is None


def test_error_rule_recurrence_counts_dates_per_signature(db_pool_greenfield) -> None:
    engine = db_pool_greenfield
    group_id, model_ids = _seed_group(engine)
    for model_id, as_of in zip(model_ids, DATES):
        _leaf(engine, model_id, as_of, "fp", 0, "a>|b<=", 40, 20, None)  # every date
    _leaf(engine, model_ids[0], DATES[0], "fp", 1, "c>", 60, 6, None)  # one date
    # a second leaf with the same signature on one date counts that date once
    _leaf(engine, model_ids[0], DATES[0], "fp", 2, "a>|b<=", 80, 20, None)
    # rows before 0027 carry no signature: they count toward the total, not to a rule
    _leaf(engine, model_ids[1], DATES[1], "fp", 3, None, 10, 1, None)
    # other kinds and other cuts are not this query's rows
    _leaf(engine, model_ids[2], DATES[2], "fn", 0, "c>", 70, 7, None)

    with engine.connection() as conn:
        rows = conn.execute(
            "select * from triage.error_rule_recurrence(%(g)s, %(p)s)",
            {"g": group_id, "p": PARAMETER},
        ).fetchall()
        other_cut = conn.execute(
            "select * from triage.error_rule_recurrence(%(g)s, '1_pct', 'fp')",
            {"g": group_id},
        ).fetchall()
    assert other_cut == []
    by_sig = {r["signature"]: r for r in rows}
    assert set(by_sig) == {"a>|b<=", "c>"}
    assert [r["signature"] for r in rows] == ["a>|b<=", "c>"], "most recurrent first"

    recurring = by_sig["a>|b<="]
    assert (recurring["n_dates"], recurring["n_dates_total"]) == (3, 3)
    assert recurring["mean_support"] == pytest.approx((40 * 3 + 80) / 4)
    assert recurring["mean_error_rate"] == pytest.approx((0.5 * 3 + 0.25) / 4)
    assert recurring["example_rule"] == f"a>|b<= @ {DATES[0]} #2", "largest leaf"
    assert (by_sig["c>"]["n_dates"], by_sig["c>"]["n_dates_total"]) == (1, 3)
