"""Near-miss bands, prototypes and the Jaccard sweep (migration 0037).

One DirtyDuck-shaped classification model scores 500 facilities on two dates from one
matrix of inspection features. On D1 the near-miss band (ranks 101–200) has the same base
rate as the selected top 100, 0.30, and the rest has a lower one. A block of ten tied
scores straddles the cut at rank 100, so the entity_id tie-break decides which of them are
selected; every row of the block is a labeled negative, which keeps the planted counts.
50 rows are unlabeled. A regression_ranking twin reads the same positives as
``outcome > 0`` with a continuous target, and a regression model is refused.

Each value is checked against a numpy reference written here: the population ranks by
score descending then entity_id, the bands by rank, and the prototypes by brute force over
every row of each class.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import date
from typing import Any, Optional

import numpy as np
import pytest
from typer.testing import CliRunner

import triage.cli as cli
from triage.diagnostics import readers

LABEL_TIMESPAN = "6 months"
D1, D2 = date(2016, 1, 1), date(2016, 4, 1)
N, K = 500, 100
#: The ranks of D1 that share one score; all are labeled negatives.
TIE_BLOCK = range(96, 106)
FEATURES = [
    "inspections_entity_id_1y_total_count",
    "inspections_entity_id_3y_total_count",
    "violations_entity_id_1y_severity_avg",
    "violations_entity_id_3y_severity_max",
    "results_entity_id_1y_fail_count",
    "results_entity_id_1y_pass_count",
    "risk_high_flag",
    "facility_type_restaurant",
    "facility_type_school",
    "zip_code_60622",
    "days_since_last_inspection",
    "license_age_years",
]
#: Constant over every row: no z-score, so left out of the distance.
CONSTANT = "city_chicago"

_RNG = np.random.default_rng(12)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _labels_by_rank(rng: np.random.Generator) -> list[Optional[float]]:
    """The label at each rank 1..N of D1: 27 positives in 90 labeled rows of each of the
    top two bands, 14 in 270 of the rest, and negatives across the tie block."""
    labels: list[Optional[float]] = [0.0] * N
    for lo, hi, unlabeled, positives in (
        (1, 100, 10, 27),
        (101, 200, 10, 27),
        (201, 500, 30, 14),
    ):
        free = [r for r in range(lo, hi + 1) if r not in TIE_BLOCK]
        picked = rng.choice(free, unlabeled + positives, replace=False)
        for r in picked[:unlabeled]:
            labels[r - 1] = None
        for r in picked[unlabeled:]:
            labels[r - 1] = 1.0
    return labels  # fmt: skip


def _d1() -> tuple[dict[int, float], dict[int, Optional[float]]]:
    """Scores and labels of D1, keyed by entity: rank r holds entity PERM[r]."""
    perm = _RNG.permutation(N) + 1
    labels = _labels_by_rank(_RNG)
    scores, outcomes = {}, {}
    for r in range(1, N + 1):
        tied = r in TIE_BLOCK
        scores[int(perm[r - 1])] = 1.0 - (TIE_BLOCK.start if tied else r) / 1000.0
        outcomes[int(perm[r - 1])] = labels[r - 1]
    return scores, outcomes


def _d2() -> tuple[dict[int, float], dict[int, Optional[float]]]:
    """D2: an unrelated ranking with a 20% base rate, every row labeled."""
    scores = {e: float(s) for e, s in zip(range(1, N + 1), _RNG.random(N), strict=True)}
    outcomes: dict[int, Optional[float]] = {
        e: float(_RNG.random() < 0.2) for e in range(1, N + 1)
    }
    return scores, outcomes


SCORES = {D1: _d1(), D2: _d2()}
#: The matrix: one row per entity and date, features drawn per row.
MATRIX = {
    as_of: {
        f: (_RNG.poisson(3, N) if "count" in f or "days" in f else _RNG.normal(0, 1, N))
        .astype(float)
        .round(3)
        for f in FEATURES
    }
    for as_of in (D1, D2)
}
#: A continuous target with the classification positives as its nonzero rows.
AMOUNT = {e: float(_RNG.gamma(2.0, 50.0)) for e in range(1, N + 1)}


def _outcomes(as_of: date, problem_type: str) -> dict[int, Optional[float]]:
    binary = SCORES[as_of][1]
    if problem_type == "classification":
        return binary
    return {e: None if y is None else y * AMOUNT[e] for e, y in binary.items()}


def seed_model(
    pool,
    tmp_path,
    *,
    tag: str,
    problem_type: str = "classification",
    noise: float = 0.0,
    missing_feature: bool = False,
    group_id: Optional[int] = None,
) -> int:
    """One model scored on D1 and D2 from its own matrix and labels artifacts.

    ``noise`` perturbs D1's scores (a second model for the overlap); ``missing_feature``
    blanks one feature value of D1.
    """
    import polars as pl

    frames = []
    for as_of in (D1, D2):
        columns: dict[str, Any] = {f: list(v) for f, v in MATRIX[as_of].items()}
        if missing_feature and as_of == D1:
            columns[FEATURES[0]][0] = None
        frames.append(
            pl.DataFrame(
                {
                    "entity_id": list(range(1, N + 1)),
                    "as_of_date": [as_of] * N,
                    **columns,
                    CONSTANT: [1.0] * N,
                }
            )
        )
    uri = str(tmp_path / f"near-miss-{tag}.parquet")
    pl.concat(frames).write_parquet(uri)
    matrix_uuid = str(uuid.uuid4())
    noise_by_entity = np.random.default_rng(len(tag)).normal(0, noise, N + 1)
    with pool.connection() as conn:
        conn.execute(
            "insert into triage.experiments (experiment_hash, config, problem_type)"
            " values (%(x)s, '{}'::jsonb, %(t)s)",
            {"x": f"exp-{tag}", "t": problem_type},
        )
        run_id = conn.execute(
            "insert into triage.runs (experiment_hash, profile)"
            " values (%(x)s, 'local') returning run_id",
            {"x": f"exp-{tag}"},
        ).fetchone()["run_id"]
        conn.execute(
            "insert into triage.artifacts (artifact_id, logical_id, kind, config) values"
            " (%(l)s, %(l)s, 'labels', '{}'::jsonb), (%(mx)s, %(mx)s, 'matrix', '{}'::jsonb),"
            " (%(m)s, %(m)s, 'model', '{}'::jsonb)",
            {"l": f"lbl-{tag}", "mx": f"mx-{tag}", "m": f"model-{tag}"},
        )  # fmt: skip
        conn.execute(
            "insert into triage.artifact_inputs (artifact_id, parent_id)"
            " values (%(mx)s, %(l)s)",
            {"mx": f"mx-{tag}", "l": f"lbl-{tag}"},
        )
        conn.execute(
            "insert into triage.matrices (matrix_uuid, artifact_id, matrix_kind,"
            " storage_uri, feature_names, label_timespan) values (%(u)s, %(a)s, 'test',"
            " %(uri)s, %(f)s, cast(%(ts)s as interval))",
            {
                "u": matrix_uuid,
                "a": f"mx-{tag}",
                "uri": uri,
                "f": [*FEATURES, CONSTANT],
                "ts": LABEL_TIMESPAN,
            },
        )
        if group_id is None:
            group_id = conn.execute(
                "insert into triage.model_groups"
                " (model_group_hash, model_type, hyperparameters, feature_list)"
                " values (%(h)s, 'sklearn.ensemble.RandomForestClassifier', '{}'::jsonb,"
                " %(f)s) returning model_group_id",
                {"h": f"mg-{tag}", "f": FEATURES},
            ).fetchone()["model_group_id"]
        model_id = conn.execute(
            "insert into triage.models (model_group_id, model_hash, run_id,"
            " train_end_time, training_label_timespan) values (%(g)s, %(a)s, %(r)s,"
            " date '2015-12-01', cast(%(ts)s as interval)) returning model_id",
            {"g": group_id, "a": f"model-{tag}", "r": run_id, "ts": LABEL_TIMESPAN},
        ).fetchone()["model_id"]
        with conn.cursor() as cur:
            for as_of in (D1, D2):
                scores = SCORES[as_of][0]
                cur.executemany(
                    "insert into triage.predictions (model_id, entity_id, as_of_date,"
                    " split_kind, score, matrix_uuid)"
                    " values (%(m)s, %(e)s, %(d)s, 'test', %(s)s, %(u)s)",
                    [
                        {"m": model_id, "e": e, "d": as_of, "u": matrix_uuid,
                         "s": s + (noise_by_entity[e] if as_of == D1 else 0.0)}
                        for e, s in scores.items()
                    ],
                )  # fmt: skip
                cur.executemany(
                    "insert into triage.labels (label_hash, entity_id, as_of_date,"
                    " label_timespan, outcome) values (%(l)s, %(e)s, %(d)s,"
                    " cast(%(ts)s as interval), %(o)s)",
                    [
                        {"l": f"lbl-{tag}", "e": e, "d": as_of, "ts": LABEL_TIMESPAN,
                         "o": o}
                        for e, o in _outcomes(as_of, problem_type).items()
                        if o is not None
                    ],
                )  # fmt: skip
    return model_id


@pytest.fixture
def classifier(db_pool_greenfield, tmp_path) -> int:
    return seed_model(db_pool_greenfield, tmp_path, tag="clf")


# ----------------------------------------------------------------- numpy references


def _k(parameter: str, n: int) -> int:
    raw, unit = parameter.split("_")
    k = math.floor(float(raw)) if unit == "abs" else math.ceil(float(raw) / 100 * n)
    return max(0, min(k, n))


def ranked(scores: dict[int, float]) -> list[int]:
    """Entities in population rank order: score descending, entity_id ascending."""
    entities = np.array(sorted(scores))
    values = np.array([scores[e] for e in entities])
    return entities[np.lexsort((entities, -values))].tolist()


def ref_bands(
    as_of: date, parameter: str, width: Optional[str], problem_type: str
) -> list[dict[str, Any]]:
    scores = SCORES[as_of][0]
    outcomes = _outcomes(as_of, problem_type)
    order = ranked(scores)
    k, w = _k(parameter, N), _k(width or parameter, N)
    bands = []
    for name, lo, hi in (
        ("selected", 0, k),
        ("near_miss", k, min(k + w, N)),
        ("rest", min(k + w, N), N),
    ):
        members = order[lo:hi]
        labeled = [y for e in members if (y := outcomes[e]) is not None]
        positive = [y for y in labeled if y > 0]
        bands.append(
            {
                "band": name,
                "first_rank": lo + 1 if members else None,
                "last_rank": hi if members else None,
                "n": len(members),
                "n_labeled": len(labeled),
                "n_positive": len(positive),
                "base_rate": len(positive) / len(labeled) if labeled else None,
                "mean_outcome": float(np.mean(labeled)) if labeled else None,
                "mean_score": float(np.mean([scores[e] for e in members]))
                if members
                else None,
            }
        )
    return bands


def _same(stored: dict[str, Any], reference: dict[str, Any]) -> None:
    for key, expected in reference.items():
        if isinstance(expected, float):
            assert stored[key] == pytest.approx(expected, rel=1e-12), key
        else:
            assert stored[key] == expected, key


def ref_prototypes(
    as_of: date, parameter: str, n: int, problem_type: str = "classification"
) -> dict[str, dict[str, Any]]:
    """Brute force: z-score over the labeled rows, every distance by a Python loop."""
    scores = SCORES[as_of][0]
    outcomes = _outcomes(as_of, problem_type)
    order = ranked(scores)
    rank = {e: i + 1 for i, e in enumerate(order)}
    k = _k(parameter, N)
    labeled = [e for e in range(1, N + 1) if outcomes[e] is not None]
    matrix = MATRIX[as_of]
    features = [f for f in FEATURES if np.std([matrix[f][e - 1] for e in labeled]) > 0]
    mean = {f: sum(matrix[f][e - 1] for e in labeled) / len(labeled) for f in features}
    std = {
        f: math.sqrt(
            sum((matrix[f][e - 1] - mean[f]) ** 2 for e in labeled) / len(labeled)
        )
        for f in features
    }
    z = {e: [(matrix[f][e - 1] - mean[f]) / std[f] for f in features] for e in labeled}

    def cls(e: int) -> str:
        y = outcomes[e]
        assert y is not None
        if rank[e] <= k:
            return "tp" if y > 0 else "fp"
        return "fn" if y > 0 else "tn"

    out: dict[str, dict[str, Any]] = {}
    for c in ("tp", "fp", "fn", "tn"):
        members = [e for e in labeled if cls(e) == c]
        if not members:
            continue
        centroid = [
            sum(z[e][j] for e in members) / len(members) for j in range(len(features))
        ]
        dist = {
            e: math.sqrt(
                sum((z[e][j] - centroid[j]) ** 2 for j in range(len(features)))
            )
            for e in members
        }
        nearest = sorted(members, key=lambda e: (dist[e], e))[:n]
        out[c] = {
            "size": len(members),
            "entities": nearest,
            "distances": [dist[e] for e in nearest],
            "deviations": {
                e: sorted(
                    ((f, z[e][j] - centroid[j]) for j, f in enumerate(features)),
                    key=lambda fd: (-abs(fd[1]), fd[0]),
                )[:10]
                for e in nearest
            },
        }
    return out


# ------------------------------------------------------------------------- near miss


def test_the_planted_near_miss_band_has_the_selected_base_rate(
    db_pool_greenfield, classifier
):
    rows = readers.near_miss(db_pool_greenfield, classifier, "100_abs", as_of_date=D1)
    assert [r["band"] for r in rows] == ["selected", "near_miss", "rest"]
    selected, near, rest = rows
    assert (selected["n"], selected["n_labeled"], selected["n_positive"]) == (
        100,
        90,
        27,
    )
    assert (near["n"], near["n_labeled"], near["n_positive"]) == (100, 90, 27)
    assert selected["base_rate"] == near["base_rate"] == pytest.approx(0.30)
    assert rest["base_rate"] == pytest.approx(14 / 270)
    for stored, reference in zip(
        rows, ref_bands(D1, "100_abs", None, "classification"), strict=True
    ):
        _same(stored, reference)


@pytest.mark.parametrize(
    ("parameter", "width"),
    [
        ("100_abs", "50_abs"),
        ("10_pct", None),
        ("5_pct", "20_pct"),
        ("400_abs", "200_abs"),
    ],
)
def test_bands_match_numpy_on_every_date(
    db_pool_greenfield, classifier, parameter, width
):
    rows = readers.near_miss(db_pool_greenfield, classifier, parameter, width)
    assert [r["as_of_date"] for r in rows] == [D1] * 3 + [D2] * 3
    for as_of in (D1, D2):
        stored = [r for r in rows if r["as_of_date"] == as_of]
        for got, reference in zip(
            stored, ref_bands(as_of, parameter, width, "classification"), strict=True
        ):
            _same(got, reference)
    if parameter == "400_abs":  # the near miss reaches the end: the rest is empty
        rest = rows[2]
        assert (rest["n"], rest["first_rank"], rest["base_rate"]) == (0, None, None)


def test_regression_ranking_reads_positive_as_outcome_above_zero(
    db_pool_greenfield, tmp_path
):
    model = seed_model(
        db_pool_greenfield, tmp_path, tag="rr", problem_type="regression_ranking"
    )
    rows = readers.near_miss(db_pool_greenfield, model, "100_abs", as_of_date=D1)
    for got, reference in zip(
        rows, ref_bands(D1, "100_abs", None, "regression_ranking"), strict=True
    ):
        _same(got, reference)
    assert rows[0]["base_rate"] == pytest.approx(0.30)
    assert rows[0]["mean_outcome"] > 1.0  # the amount, not the share


def test_an_unscored_date_and_an_unknown_model(db_pool_greenfield, classifier):
    assert readers.near_miss(db_pool_greenfield, classifier, "100_abs", None, "test",
                             date(2020, 1, 1)) == []  # fmt: skip
    with pytest.raises(readers.NotFoundError, match="model 999999 not found"):
        readers.near_miss(db_pool_greenfield, 999999, "100_abs")


# ------------------------------------------------------------------ near-miss crosstabs


def test_near_miss_crosstabs_compare_the_next_k_rows(db_pool_greenfield, classifier):
    from triage.diagnostics import compute_crosstabs

    pool = db_pool_greenfield
    compute_crosstabs(pool, classifier, parameter="100_abs", as_of_date=D1)
    rest_before = readers.crosstabs(pool, classifier, "100_abs", limit=100)
    written = compute_crosstabs(
        pool, classifier, parameter="100_abs", as_of_date=D1, against="near_miss"
    )
    assert written == 3 * (len(FEATURES) + 1)
    assert readers.crosstabs(pool, classifier, "100_abs", limit=100) == rest_before

    order = ranked(SCORES[D1][0])
    selected, near = order[:K], order[K : 2 * K]
    with pool.connection() as conn:
        stored = {
            r["feature"]: r
            for r in conn.execute(
                "select feature, selected_value, rest_value from triage.crosstabs"
                " where model_id = %(m)s and comparison = 'near_miss' and stat = 'mean'",
                {"m": classifier},
            ).fetchall()
        }
    for feature in FEATURES:
        values = MATRIX[D1][feature]
        assert stored[feature]["selected_value"] == pytest.approx(
            np.mean([values[e - 1] for e in selected])
        )
        assert stored[feature]["rest_value"] == pytest.approx(
            np.mean([values[e - 1] for e in near])
        )
    by_near = readers.crosstabs(pool, classifier, "100_abs", comparison="near_miss")
    assert by_near and all(r["feature"] in stored for r in by_near)
    with pytest.raises(ValueError, match="no such band"):
        compute_crosstabs(pool, classifier, abs_error_cut="10_pct", against="near_miss")


# ------------------------------------------------------------------------ prototypes


def test_prototypes_are_the_centroid_nearest_entities(db_pool_greenfield, classifier):
    from triage.diagnostics.prototypes import compute_prototypes

    pool = db_pool_greenfield
    computed = compute_prototypes(pool, classifier, "100_abs", n=3)
    assert computed.dates == [str(D1), str(D2)]
    assert computed.dropped_features == {str(D1): [CONSTANT], str(D2): [CONSTANT]}
    for as_of in (D1, D2):
        stored = readers.prototypes(pool, classifier, "100_abs", as_of)
        reference = ref_prototypes(as_of, "100_abs", 3)
        assert [r["class"] for r in stored] == [
            c for c in ("tp", "fp", "fn", "tn") if c in reference for _ in range(3)
        ]
        for c, ref in reference.items():
            rows = [r for r in stored if r["class"] == c]
            assert [r["entity_id"] for r in rows] == ref["entities"], (as_of, c)
            assert [r["rank"] for r in rows] == [1, 2, 3]
            assert {r["class_size"] for r in rows} == {ref["size"]}
            for r, dist in zip(rows, ref["distances"], strict=True):
                assert r["distance"] == pytest.approx(dist, rel=1e-9)
                expected = ref["deviations"][r["entity_id"]]
                assert [d["feature"] for d in r["deviations"]] == [
                    f for f, _ in expected
                ]
                for d, (_, gap) in zip(r["deviations"], expected, strict=True):
                    assert d["deviation"] == pytest.approx(gap, rel=1e-9, abs=1e-12)
                    assert d["z"] - d["centroid_z"] == pytest.approx(d["deviation"])
    # the class sizes on D1 follow the plant: 27 TP + 63 FP selected, 41 FN + 319 TN
    sizes = {
        r["class"]: r["class_size"]
        for r in readers.prototypes(pool, classifier, "100_abs", D1)
    }
    assert sizes == {"tp": 27, "fp": 63, "fn": 41, "tn": 319}


def test_a_rerun_with_fewer_prototypes_leaves_no_stale_rank(
    db_pool_greenfield, classifier
):
    from triage.diagnostics.prototypes import compute_prototypes

    pool = db_pool_greenfield
    compute_prototypes(pool, classifier, "100_abs", n=5, as_of_date=D1)
    compute_prototypes(pool, classifier, "100_abs", n=1, as_of_date=D1)
    rows = readers.prototypes(pool, classifier, "100_abs", D1)
    assert [(r["class"], r["rank"]) for r in rows] == [
        ("tp", 1),
        ("fp", 1),
        ("fn", 1),
        ("tn", 1),
    ]
    assert readers.prototypes(pool, classifier, "100_abs", D2) == []


def test_regression_ranking_prototypes_read_outcome_above_zero(
    db_pool_greenfield, tmp_path
):
    from triage.diagnostics.prototypes import compute_prototypes

    pool = db_pool_greenfield
    model = seed_model(pool, tmp_path, tag="rr", problem_type="regression_ranking")
    compute_prototypes(pool, model, "100_abs", n=2, as_of_date=D1)
    reference = ref_prototypes(D1, "100_abs", 2, "regression_ranking")
    stored = readers.prototypes(pool, model, "100_abs", D1)
    for c, ref in reference.items():
        assert [r["entity_id"] for r in stored if r["class"] == c] == ref["entities"]


def test_a_missing_feature_value_refuses_the_date(db_pool_greenfield, tmp_path):
    from triage.diagnostics.prototypes import compute_prototypes

    model = seed_model(db_pool_greenfield, tmp_path, tag="gap", missing_feature=True)
    with pytest.raises(ValueError, match=f"{FEATURES[0]} have missing values"):
        compute_prototypes(db_pool_greenfield, model, "100_abs", as_of_date=D1)


# ---------------------------------------------------------------------------- sweep

SWEEP = ["50_abs", "100_abs", "200_abs", "10_pct"]


def test_the_sweep_returns_one_row_per_cut(db_pool_greenfield, tmp_path, classifier):
    pool = db_pool_greenfield
    other = seed_model(pool, tmp_path, tag="noisy", noise=0.05)
    rows = readers.list_overlap_sweep(pool, classifier, other, SWEEP)
    assert [(r["parameter"], r["as_of_date"]) for r in rows] == [
        (cut, d) for cut in SWEEP for d in (D1, D2)
    ]
    noise = np.random.default_rng(len("noisy")).normal(0, 0.05, N + 1)
    a = ranked(SCORES[D1][0])
    b = ranked({e: s + noise[e] for e, s in SCORES[D1][0].items()})
    for r in (r for r in rows if r["as_of_date"] == D1):
        k = _k(r["parameter"], N)
        top_a, top_b = set(a[:k]), set(b[:k])
        assert (r["k_a"], r["k_b"]) == (k, k)
        assert r["n_intersection"] == len(top_a & top_b)
        assert r["jaccard"] == pytest.approx(len(top_a & top_b) / len(top_a | top_b))
    for r in (r for r in rows if r["as_of_date"] == D2):  # same scores on D2
        assert r["jaccard"] == pytest.approx(1.0)
    with pytest.raises(ValueError, match="more than once"):
        readers.list_overlap_sweep(pool, classifier, other, ["50_abs", "50_abs"])


def test_the_sweep_plot_draws_one_line_per_pair_and_date(
    db_pool_greenfield, tmp_path, classifier
):
    from triage import plots

    pool = db_pool_greenfield
    with pool.connection() as conn:
        group_a = conn.execute(
            "select model_group_id from triage.models where model_id = %(m)s",
            {"m": classifier},
        ).fetchone()["model_group_id"]
    other = seed_model(pool, tmp_path, tag="noisy", noise=0.05)
    with pool.connection() as conn:
        group_b = conn.execute(
            "select model_group_id from triage.models where model_id = %(m)s",
            {"m": other},
        ).fetchone()["model_group_id"]
    result = plots.overlap_sweep(pool, group_a, group_b, SWEEP)
    assert result.data == {
        f"model {classifier} vs model {other}": readers.list_overlap_sweep(
            pool, classifier, other, SWEEP
        )
    }
    assert result.summary.startswith("2 pair(s) of models and test dates over 4 cut(s)")
    axes = result.figure.axes[0]
    assert len(axes.get_lines()) == 2
    assert np.asarray(axes.get_lines()[0].get_xdata()).tolist() == [50, 50, 100, 200]


# ------------------------------------------------------------------------------ CLI


@pytest.fixture
def run_cli(db_url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def run(*args: str) -> tuple[int, str]:
        result = CliRunner().invoke(
            cli.app, list(args), env={"DATABASE_URL": db_url, "COLUMNS": "200"}
        )
        return result.exit_code, " ".join(_ANSI.sub("", result.output).split())

    return run


def test_cli_near_miss_prints_the_bands(run_cli, db_pool_greenfield, classifier):
    code, text = run_cli("postmodel", "near-miss", str(classifier), "-p", "100_abs",
                         "--date", str(D1))  # fmt: skip
    assert code == 0, text
    assert "Near miss — model" in text and "top 100_abs, band 100_abs" in text
    assert "Near miss 101–200 100 90 27 30.0%" in text
    code, text = run_cli("postmodel", "near-miss", str(classifier), "--width",
                         "50_abs", "--json")  # fmt: skip
    assert code == 0, text
    payload = json.loads(text[text.index("[") :])  # after the "Using database" line
    assert len(payload) == 6 and payload[1]["last_rank"] == 150
    code, text = run_cli("postmodel", "near-miss", str(classifier), "-p", "ten")
    assert code == 2 and "malformed threshold parameter" in text


def test_cli_prototypes_persist_and_print(run_cli, db_pool_greenfield, classifier):
    code, text = run_cli("postmodel", "prototypes", str(classifier), "-p", "100_abs",
                         "-n", "2", "--date", str(D1))  # fmt: skip
    assert code == 0, text
    assert "8 prototype row(s) persisted" in text
    assert "1 feature(s) constant over the labeled rows" in text
    first = ref_prototypes(D1, "100_abs", 2)["tp"]["entities"][0]
    assert f"TP 27 1 {first} " in text
    code, text = run_cli("postmodel", "prototypes", str(classifier), "--json")
    assert code == 0, text
    payload = json.loads(text[text.index("[") :])
    assert len(payload) == 2 * 3 * 4  # two dates, four classes, n = 3


def test_cli_refuses_a_regression_model(run_cli, db_pool_greenfield, tmp_path):
    model = seed_model(
        db_pool_greenfield, tmp_path, tag="reg", problem_type="regression"
    )
    code, text = run_cli("postmodel", "near-miss", str(model))
    assert code == 2
    assert (
        f"is regression. For a regression model use triage plot gains {model}" in text
    )
    code, text = run_cli("postmodel", "prototypes", str(model))
    assert code == 2
    assert f"triage postmodel crosstabs {model} --by abs-error" in text


def test_cli_crosstabs_against_near_miss(run_cli, db_pool_greenfield, classifier):
    code, text = run_cli("postmodel", "crosstabs", str(classifier), "-p", "100_abs",
                         "--against", "near-miss")  # fmt: skip
    assert code == 0, text
    assert "selected vs near miss" in text
    with db_pool_greenfield.connection() as conn:
        counts = conn.execute(
            "select comparison, count(*) as n from triage.crosstabs"
            " where model_id = %(m)s group by comparison",
            {"m": classifier},
        ).fetchall()
    assert counts == [{"comparison": "near_miss", "n": 2 * 3 * (len(FEATURES) + 1)}]
    code, text = run_cli("postmodel", "crosstabs", str(classifier), "--against",
                         "near-miss", "--by", "abs-error")  # fmt: skip
    assert code == 2 and "Use one or the other" in text
    code, text = run_cli("postmodel", "crosstabs", str(classifier), "--against", "next")
    assert code == 2 and "expected rest or near-miss" in text


def test_cli_compare_sweep(run_cli, db_pool_greenfield, tmp_path, classifier):
    other = seed_model(db_pool_greenfield, tmp_path, tag="noisy", noise=0.05)
    code, text = run_cli("postmodel", "compare", str(classifier), str(other),
                         "--sweep", ",".join(SWEEP))  # fmt: skip
    assert code == 0, text
    assert "List overlap as k grows" in text
    for cut in SWEEP:
        assert f" {cut} {D1} " in text
    code, text = run_cli("postmodel", "compare", str(classifier), str(other),
                         "--sweep", "50_abs", "-p", "100_abs")  # fmt: skip
    assert code == 2 and "not both" in text
    code, text = run_cli("postmodel", "compare", str(classifier), str(other),
                         "--sweep", "50_abs,fifty")  # fmt: skip
    assert code == 2 and "comma-separated cuts" in text
    # without --sweep, the one-cut table is unchanged
    code, text = run_cli("postmodel", "compare", str(classifier), str(other))
    assert code == 0 and "@ 100_abs" in text


def test_problem_type_table_names_the_cut_class_commands():
    from triage import problem_types

    for command in ("postmodel near-miss", "postmodel prototypes"):
        assert problem_types.refusal(command, 7, "classification") is None
        assert problem_types.refusal(command, 7, "regression_ranking") is None
        survival = problem_types.refusal(command, 7, "survival")
        assert survival is not None and "--horizon H" in survival
