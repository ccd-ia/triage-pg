"""Greenfield matrix assembler — the triage-pg ↔ featurizer seam (ADR-0008, ADR-0009, ADR-0015).

This is the most load-bearing adapter: it turns a *featurizer ER-graph config* + a built
*cohort* + built *labels* into a Parquet design matrix registered in the artifact DAG. It is
the home of two ADR boundaries:

* **The featurizer seam (ADR-0008, adapter-spec §2).** featurizer owns the ER-graph + DFS
  and renders a *dense* ``(as_of_date × target-entity)`` feature matrix; triage-pg owns only
  the seam — it materializes the split's ``as_of_dates`` (the runtime table featurizer reads
  by bare name), passes the config through with ``as_of_boundary: exclusive`` (triage's
  strictly-before point-in-time rule), and then applies the cohort + labels as INNER-JOIN
  *selection masks* on ``(entity_id, as_of_date[, label_timespan])``. Triage concepts never
  leak into featurizer.
* **The imputation leakage boundary (ADR-0009, adapter-spec §3).** featurizer emits
  NULL-preserving features + ``__missing`` flags + fit-free zero-fills; the adapter does the
  *fit-based* fills (mean/median/mode/binary_mode), and those statistics are computed over
  the **train split only**, persisted into ``triage.matrices.metadata``, and **reused** for
  the test matrix — never recomputed on the test split. The train→test parent edge in the
  DAG *is* that leakage boundary.

Output path — Arrow/Polars, not one-pass SQL (documented choice)
----------------------------------------------------------------
adapter-spec §2.8/§3.1 *prefer* "SQL → Parquet in one pass over ``Featurizer.query``"; they
also permit Arrow/Polars "if you keep the leakage boundary intact." We assemble in
**pyarrow + Polars**, deliberately:

1. featurizer feature names render as ``COUNT(orders.amount|interval=P30D)`` — parens, pipes
   and dots — which are punishing to round-trip through a wrapped, double-quoted SQL
   subquery; as Arrow/Polars column names they are just strings.
2. Wide configs shard into ``query_groups`` (multiple SQLs that re-join on the keys);
   ``Featurizer.to_arrow`` already re-joins those into one Table, so consuming Arrow avoids
   re-implementing the group merge in SQL.
3. ``to_arrow(impute=True)`` performs featurizer's *fit-free* pass for the aggregations it
   classifies as count-like (→ 0), leaves measures NULL, and emits ``<feature>__missing``
   0/1 flags. featurizer's count-like classification is **narrower** than the triage
   imputation policy, though — e.g. recency/tenure *time-since* primitives are left NULL — so
   the adapter applies the policy's *own* fit-free fills (:func:`_apply_fit_free`:
   zero/constant per :class:`ImputationPolicy`) on top, for every feature whose rule is
   fit-free. It does **not** re-spell featurizer's count-like classification in SQL (that
   would duplicate it, against ADR-0008); it only fills the residual the policy declares. The
   adapter then adds the *fit-based* fills (the leakage boundary), and a fail-fast guard
   (:func:`_check_no_nulls_remain`) asserts no NULL survives — so a NaN can never silently
   reach the model (ADR-0009 "imputation required"; trees tolerate NaN and would mask it).

The leakage boundary is preserved identically in Arrow: the fit-based statistic is computed
over the *train Table's rows only*, persisted, and on the test side read back and applied with
``fill_null`` — never recomputed from test rows, never per ``as_of_date``. Fit-free fills need
no statistic, so they are applied identically on train and test (leakage-safe).

Lifecycle (mirrors :mod:`triage.adapters.cohort` / :mod:`~.labels`)
-------------------------------------------------------------------
Two artifact nodes are registered per matrix build:

* a ``feature_group`` node — identity over the canonical featurizer sub-config + the cohort
  parent + source pins + ``engine_versions_for('feature_group')`` (which adds featurizer);
* a ``matrix`` node — identity over ``{temporal_config.canonical(), feature_group canonical,
  imputation_policy.canonical(), matrix_kind, label_timespan}``, parents ``[feature_group,
  cohort, labels]`` (+ the train matrix for a test matrix), ``engine_versions_for('matrix')``
  (triage-pg only). ``matrices.matrix_uuid = as_uuid(matrix artifact_id)`` (ADR-0015).

On each: ``derive`` → ``cache_hit`` (reuse + ``record_use`` on hit) → ``begin_artifact`` →
assemble → ``mark_built`` → ``record_use``, with ``mark_failed`` + re-raise on any error.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from triage.util.db import DictRowPool

from triage.adapters.imputation import ImputationPolicy, ImputationRule
from triage.adapters.temporal import TemporalConfig
from triage.artifacts import (
    FEATURE_GROUP_OUTPUT_REF,
    begin_artifact,
    cache_hit,
    get_artifact,
    mark_built,
    mark_failed,
    record_use,
)
from triage.derivation import Derivation, as_uuid, derive, engine_versions_for
from triage.logging import get_logger
from triage.profiles.protocols import StorageAdapter
from triage.profiles.storage import write_parquet

logger = get_logger(__name__)

__all__ = [
    "build_matrix",
    "MatrixResult",
    "MATRIX_KIND",
    "FEATURE_GROUP_KIND",
]

MATRIX_KIND = "matrix"
FEATURE_GROUP_KIND = "feature_group"


def _missing_suffix() -> str:
    """featurizer's missing-indicator suffix (adapter-spec §3.3).

    Read from the package so we never drift from the engine's constant; falls back to the
    documented literal if the symbol is ever renamed (a loud-enough place to notice).
    """
    try:
        from featurizer import MISSING_INDICATOR_SUFFIX

        return MISSING_INDICATOR_SUFFIX
    except ImportError:
        return "__missing"


# The leading key columns featurizer's Arrow output carries (adapter-spec §2.3): the
# as_of_date and the target entity's id. These are never features, never imputed.
_AS_OF_COL = "as_of_date"

# How the fitted per-feature statistics are stored under triage.matrices.metadata.
# {"fit_based_stats": {"<feature>": {"stat": "mean", "value": 12.3}, ...}}.
_FIT_STATS_KEY = "fit_based_stats"

# Train-fitted categorical code maps, stored alongside the fit stats (adapter-spec §4):
# {"cat_encodings": {"<feature>": {"<category>": <code int>, ...}, ...}}. Code 0 is reserved
# for unknown (unseen-at-test / NULL). This is the *learned-vocabulary* path — a fit-based
# transform fitted on the train split only and reused for test (ADR-0009 extension).
_CAT_ENC_KEY = "cat_encodings"

# Above this many distinct *train* categories an ordinal-encoded column is almost certainly an
# identifier (or a leakage/overfit risk). We still ordinal-encode it (one column, no width
# blow-up) but warn loudly so it gets a featurizer ``role: identifier``. One-hot is never
# auto-applied by the adapter (that is the declared/fixed-vocabulary path, in featurizer).
_MAX_CAT_CARDINALITY = 100


@dataclass(frozen=True)
class MatrixResult:
    """What :func:`build_matrix` returns: the matrix node plus its companion ids."""

    matrix_artifact_id: str
    feature_group_artifact_id: str
    storage_uri: str
    num_entities: int
    num_features: int
    feature_names: list[str]
    fit_based_stats: dict[str, dict[str, Any]]
    cache_hit: bool
    # train-fitted categorical code maps (adapter-spec §4); default-empty so existing
    # constructors are unaffected. {feature: {category: code}}, code 0 = unknown.
    cat_encodings: dict[str, dict[str, int]] = field(default_factory=dict)


def _reconstruct_derivation(
    engine: DictRowPool, artifact_id: str, what: str
) -> Derivation:
    """Re-read an upstream artifact and rebuild just enough of its Derivation to chain.

    Parents enter a child's hash by their ``id`` (strict) and ``logical_id`` (fallback);
    we reconstruct both from the stored row. Mirrors the cohort-parent reconstruction in
    :mod:`triage.adapters.labels`.
    """
    row = get_artifact(engine, artifact_id)
    if row is None:
        raise ValueError(
            f"{what} artifact {artifact_id!r} does not exist — build it before the"
            + " matrix (the matrix->parent edge requires the parent row)"
        )
    return Derivation(
        id=row["artifact_id"],
        logical_id=row["logical_id"],
        cacheable=row["cacheable"],
    )


def _featurizer_config_yaml(featurizer_config: Mapping[str, Any]) -> str:
    """Render the featurizer config with ``as_of_boundary: exclusive`` forced on.

    triage-pg requires data knowable *strictly before* ``as_of_date`` (CLAUDE.md cardinal
    rule); featurizer's default boundary is ``inclusive`` (``<=``). We override it here so
    every triage-pg featurizer run cuts on ``<`` — the smoke-tested
    ``where <ts> < aod.as_of_date`` / ``daterange(..., '[)')`` shape. A caller that sets a
    different boundary is overridden with a warning (the strict rule is not negotiable).
    """
    cfg = dict(featurizer_config)
    requested = cfg.get("as_of_boundary")
    if requested not in (None, "exclusive"):
        logger.warning(
            f"featurizer_config requested as_of_boundary={requested!r}; triage-pg forces"
            + " 'exclusive' (strictly-before point-in-time correctness, ADR-0008/§2.4)"
        )
    cfg["as_of_boundary"] = "exclusive"
    return yaml.safe_dump(cfg, sort_keys=True)


def _run_featurizer(
    db_engine: DictRowPool,
    featurizer_config: Mapping[str, Any],
    as_of_dates: Sequence[date],
):
    """Run featurizer over the split's as_of_dates; return (pyarrow.Table, target_id_col).

    Materializes ``as_of_dates`` and runs ``Featurizer.to_arrow(impute=True)`` on the *same*
    psycopg connection (the table is connection-visible), so the fit-free pass (zero-fill
    count-likes, NULL-preserve measures, ``__missing`` flags) is applied by featurizer and
    the keys ``(as_of_date, <target id>)`` are left untouched.
    """
    from featurizer import Featurizer

    config_yaml = _featurizer_config_yaml(featurizer_config)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(config_yaml)
        config_path = handle.name

    try:
        featurizer = Featurizer(config_path, validate=True)
        assert featurizer.target.id is not None  # validate=True guarantees a target id
        target_id_col = featurizer.target.id.name

        # featurizer reads ``as_of_dates`` (and the source tables) by bare name on this
        # connection (ADR-0019: a pooled psycopg3 connection passed straight through — no
        # raw_connection unwrap). The as_of_dates table is created + committed before
        # to_arrow so featurizer's own queries see it on the same connection.
        with db_engine.connection() as conn:
            with conn.cursor() as cur:
                _materialize_as_of_dates_psycopg(cur, as_of_dates)
            conn.commit()
            table = featurizer.to_arrow(connection=conn, impute=True)
            if not hasattr(table, "column_names"):  # an OrderedDict of column groups
                table = _merge_arrow_groups(table, target_id_col)
    finally:
        Path(config_path).unlink(missing_ok=True)
    return table, target_id_col


def _materialize_as_of_dates_psycopg(cur, as_of_dates: Sequence[date]) -> None:
    """(Re)create the runtime ``as_of_dates(as_of_date)`` table featurizer reads by bare name.

    adapter-spec §2.3: featurizer's rendered SQL is
    ``select aod.as_of_date, t.* from as_of_dates as aod cross join lateral (...)`` — it
    expects a table ``as_of_dates`` resolvable on the connection's search_path. The adapter
    owns this table; we drop+recreate it for the split's dates so one matrix build sees
    exactly its own as_of_dates and nothing leaks between builds on a shared db.

    It is a **TEMP** table (``pg_temp``, first on the search_path so featurizer resolves it):
    session-scoped, auto-dropped, invisible to other connections — never pollutes ``public``
    and cannot collide/race with a concurrent build on another connection (DB-audit #1).
    featurizer reads it on this *same* connection, so visibility is guaranteed.
    """
    cur.execute("drop table if exists as_of_dates")
    cur.execute("create temp table as_of_dates (as_of_date date primary key)")
    for as_of_date in as_of_dates:
        cur.execute(
            "insert into as_of_dates (as_of_date) values (%s) on conflict do nothing",
            (as_of_date,),
        )


def _merge_arrow_groups(groups, target_id_col: str):
    """Re-join column-group Tables (wide-config sharding) on the (as_of_date, id) keys."""
    import polars as pl

    keys = [_AS_OF_COL, target_id_col]
    merged: pl.DataFrame | None = None
    for table in groups.values():
        frame = pl.from_arrow(table)
        assert isinstance(frame, pl.DataFrame)  # from_arrow(Table) is a frame
        merged = frame if merged is None else merged.join(frame, on=keys, how="left")
    assert merged is not None, "sharding always yields at least one column group"
    return merged.to_arrow()


def _feature_columns(column_names: Sequence[str], target_id_col: str) -> list[str]:
    """Feature columns = everything that is neither a key nor a ``__missing`` flag."""
    keys = {_AS_OF_COL, target_id_col}
    suffix = _missing_suffix()
    return [
        name for name in column_names if name not in keys and not name.endswith(suffix)
    ]


def _numeric_dtypes():
    """The Polars numeric dtype tuple (sklearn consumes a numeric design matrix).

    Lazy import so polars stays an assembly-time dependency. Shared by the fit-free fill and the
    non-numeric leak guard so the two never drift.
    """
    import polars as pl

    return (
        pl.Int8, pl.Int16, pl.Int32, pl.Int64,
        pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
        pl.Float32, pl.Float64,
    )  # fmt: skip


def _numeric_feature_columns(frame, feature_columns: Sequence[str]) -> list[str]:
    """Drop any feature column that is not numeric *after* categorical encoding (leak guard).

    featurizer passes the target entity's ``temporal_ix`` through as a Date-typed **feature**
    column; :func:`_feature_columns` filters only the keys + ``__missing`` flags, so a raw Date
    (or any other non-numeric leak) would survive into ``feature_names`` and crash
    ``estimator.fit`` — :func:`triage.adapters.model._design_X` does
    ``frame.select(feature_names).to_numpy()`` with no dtype filter. By the time this runs
    :func:`_apply_cat_encoding` has turned every *legitimate* direct categorical into an ordinal
    ``Int32``, so anything still non-numeric is a leak. We drop it and log loudly (a Date column
    is almost always the target entity's ``temporal_ix`` reaching the feature set — give it a
    featurizer ``role: identifier`` to drop it at the source).
    """
    numeric = _numeric_dtypes()
    kept, dropped = [], []
    for feature in feature_columns:
        (kept if frame.schema.get(feature) in numeric else dropped).append(feature)
    if dropped:
        logger.warning(
            "dropping %d non-numeric feature column(s) from the matrix — %s. sklearn needs a"
            " numeric design matrix; a Date column here is typically the target entity's"
            " temporal_ix leaking into the feature set (mark it role:identifier in the"
            " featurizer config to drop it at the source).",
            len(dropped),
            [(f, str(frame.schema.get(f))) for f in dropped],
        )
    return kept


def _metric_of(feature_name: str) -> str:
    """Map a featurizer feature name to its imputation *metric* key.

    Feature names render as ``AGG(entity.col|interval=W)``; the metric is the leading
    aggregation token lower-cased (``count``, ``sum``, ``mean``, …) — the same key space the
    inherited ``aggregates_imputation`` block uses (adapter-spec §3.2). A name without a
    parenthesized agg (a passed-through direct variable) maps to ``all`` via the policy's
    fallback, so we return the name itself and let :meth:`ImputationPolicy.resolve` fall back.
    """
    head, _, _ = feature_name.partition("(")
    token = head.strip().lower()
    return token or feature_name


def _fit_statistic(frame, feature: str, rule: ImputationRule) -> float | None:
    """Compute a fit-based statistic for ``feature`` over the given (train) rows only.

    Returns ``None`` when the column is entirely null (no basis to fit) — the COALESCE then
    leaves the value null, which a downstream ``error`` rule would catch. mean/median/mode
    map to the obvious Polars reductions; ``binary_mode`` is ``mean(col) > 0.5`` rendered as
    1.0/0.0 (adapter-spec §3.2).
    """
    import polars as pl

    col = frame.get_column(feature).drop_nulls()
    if col.len() == 0:
        return None
    if rule.type == "mean":
        return float(col.mean())
    if rule.type == "median":
        return float(col.median())
    if rule.type == "mode":
        modes = col.mode()
        if modes.len() == 0:
            return None
        # deterministic tie-break: smallest modal value
        return float(modes.sort()[0])
    if rule.type == "binary_mode":
        return 1.0 if float(col.mean()) > 0.5 else 0.0
    raise ValueError(f"_fit_statistic called for non-fit-based rule {rule.type!r}")


def _apply_fit_based(
    frame,
    feature_columns: Sequence[str],
    fitted: Mapping[str, dict[str, Any]],
):
    """COALESCE each fit-based feature with its (train-fitted) statistic. Polars in/out.

    ``fitted`` is the persisted ``{feature: {"stat": ..., "value": ...}}`` map — for the
    train matrix it was just computed over these rows; for the test matrix it was read from
    the train matrix's metadata. Either way the *value* is the single source of truth here:
    we never recompute from ``frame`` (that would be the ADR-0009 leak on the test side).
    """
    import polars as pl

    exprs = []
    for feature in feature_columns:
        stat = fitted.get(feature)
        if stat is None or stat.get("value") is None:
            continue
        exprs.append(pl.col(feature).fill_null(stat["value"]).alias(feature))
    return frame.with_columns(exprs) if exprs else frame


def _apply_fit_free(
    frame,
    feature_columns: Sequence[str],
    policy: ImputationPolicy,
):
    """Fill every *fit-free* feature's NULLs per the policy (zero/constant). Polars in/out.

    featurizer's ``impute=True`` only zero-fills the aggregations it classifies as count-like;
    measures and recency/tenure *time-since* primitives are left NULL. This pass closes that
    gap for every feature whose resolved rule is fit-free, so the triage ``ImputationPolicy``
    (not featurizer's narrower heuristic) is authoritative for fit-free fills. Fit-free fills
    need no fitted statistic, so they are leakage-safe and applied identically on train and
    test (ADR-0009, adapter-spec §3). Fit-based and ``error`` features are left untouched here
    (handled by :func:`_apply_fit_based` / caught by :func:`_check_no_nulls_remain`).
    """
    import polars as pl

    numeric = (
        pl.Int8, pl.Int16, pl.Int32, pl.Int64,
        pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
        pl.Float32, pl.Float64,
    )  # fmt: skip
    exprs = []
    for feature in feature_columns:
        rule = policy.resolve(_metric_of(feature))
        if rule.kind != "fit_free":
            continue
        if rule.type in ("zero", "zero_noflag"):
            fill: Any = 0
        elif rule.type == "constant":
            fill = rule.value
        else:
            # null_category (categoricals) is already handled by _apply_cat_encoding's
            # fill_null(0); nothing to fill here for a non-categorical column.
            continue
        if frame.schema[feature] not in numeric:
            # A zero/constant fill is only meaningful for a numeric feature. A non-numeric
            # column here (e.g. a target temporal_ix date that leaked into the feature set)
            # is left for _check_no_nulls_remain to flag if it actually carries NULLs, rather
            # than crashing fill_null on an incompatible dtype.
            continue
        exprs.append(pl.col(feature).fill_null(fill).alias(feature))
    return frame.with_columns(exprs) if exprs else frame


def _categorical_features(frame, feature_columns: Sequence[str]) -> list[str]:
    """Feature columns whose dtype is string/categorical — these need encoding before sklearn.

    featurizer aggregates child-event categoricals to numbers already; what reaches here as a
    string is a *direct* target-entity categorical (e.g. ``facility_type``) that would crash
    ``estimator.fit`` on the raw value (adapter-spec §4).
    """
    import polars as pl

    string_types = (pl.Utf8, pl.String, pl.Categorical)
    return [f for f in feature_columns if frame.schema.get(f) in string_types]


def _fit_cat_encoding(train_frame, feature: str) -> dict[str, int]:
    """Train-only ``{category: code}`` map for one categorical feature (adapter-spec §4).

    Categories present in the *train* rows only, ``drop_nulls().unique().sort()`` →
    deterministic codes starting at 1 (**code 0 is reserved** for unknown: unseen-at-test and
    NULL). This is the "fit" — computed on train, persisted, reused for test, never refit.
    """
    cats = train_frame.get_column(feature).drop_nulls().unique().sort().to_list()
    if len(cats) > _MAX_CAT_CARDINALITY:
        logger.warning(
            f"categorical feature {feature!r} has {len(cats)} distinct train values"
            + f" (> {_MAX_CAT_CARDINALITY}) — likely an identifier; ordinal-encoding it"
            + " anyway, but consider marking it role:identifier in the featurizer config"
        )
    return {str(cat): code for code, cat in enumerate(cats, start=1)}


def _apply_cat_encoding(frame, encodings: Mapping[str, Mapping[str, int]]):
    """Replace each encoded string column with its ordinal code (unseen/NULL → 0). Polars in/out.

    ``encodings`` is the persisted ``{feature: {category: code}}`` map — for the train matrix
    just fitted over these rows, for the test matrix read from the train matrix's metadata.
    Either way the *map* is the single source of truth: a test-only category maps to 0, never
    refit (the same ADR-0009 leakage boundary the fit-based stats use).
    """
    import polars as pl

    exprs = []
    for feature, mapping in encodings.items():
        if feature not in frame.columns:
            continue
        exprs.append(
            pl.col(feature)
            .cast(pl.Utf8)
            .replace_strict(dict(mapping), default=0, return_dtype=pl.Int32)
            .fill_null(0)
            .alias(feature)
        )
    return frame.with_columns(exprs) if exprs else frame


def _resolve_cat_encodings(
    db_engine: DictRowPool,
    design,
    feature_columns: Sequence[str],
    train_matrix_artifact_id: str | None,
) -> dict[str, dict[str, int]]:
    """The categorical code maps for this matrix — the leakage boundary, mirroring fit stats.

    * **Train matrix**: fit a code map per categorical (string) feature over *these* rows.
    * **Test matrix**: read the train matrix's persisted maps from ``triage.matrices.metadata``
      and reuse them — never refit (refitting on test categories is the ADR-0009 leak).
    """
    if train_matrix_artifact_id is not None:
        train_meta = _existing_matrix_row(db_engine, train_matrix_artifact_id)
        encodings = (train_meta["metadata"] or {}).get(_CAT_ENC_KEY, {})
        if encodings:
            logger.info(
                f"Test matrix reusing {len(encodings)} train-fitted categorical encoding(s)"
                + " (no refit on test — ADR-0009 leakage boundary)"
            )
        return dict(encodings)

    cat_features = _categorical_features(design, feature_columns)
    encodings = {f: _fit_cat_encoding(design, f) for f in cat_features}
    if encodings:
        logger.info(
            f"Train matrix fitted {len(encodings)} categorical encoding(s):"
            + f" {sorted(encodings)}"
        )
    return encodings


def _check_no_nulls_remain(
    frame, feature_columns: Sequence[str], policy: ImputationPolicy
) -> None:
    """Fail loudly if ANY feature still has a NULL after imputation (ADR-0009, adapter-spec §3.2).

    Every feature must be fully imputed before it reaches a model — a surviving NULL becomes a
    NaN in the Parquet that some estimators (GradientBoosting, LogisticRegression) reject while
    tree ensembles silently tolerate, *masking* the gap. We refuse to ship either. A NULL here
    means one of: an ``error``-ruled feature whose data is genuinely missing; a fit-based
    feature whose statistic was un-computable (all-null on the train split); or a fit-free gap
    (which :func:`_apply_fit_free` should have closed). The message names each offender + rule.
    """
    offenders = [
        f"{feature} (rule {policy.resolve(_metric_of(feature)).type!r})"
        for feature in feature_columns
        if frame.get_column(feature).null_count() > 0
    ]
    if offenders:
        raise ValueError(
            "imputation incomplete — NULLs remain in feature(s) after the fit-free + "
            f"fit-based passes: {offenders!r}. Every feature must be imputable (ADR-0009 "
            "'imputation required'); a fit-based statistic may be un-computable (feature "
            "all-null on the train split) or an 'error' rule's data is missing."
        )


def build_matrix(
    db_engine: DictRowPool,
    run_id: str,
    featurizer_config: Mapping[str, Any],
    cohort_artifact_id: str,
    labels_artifact_id: str,
    temporal_config: TemporalConfig,
    imputation_policy: ImputationPolicy,
    matrix_kind: str,
    as_of_dates: Sequence[date],
    label_timespan: str,
    storage: StorageAdapter,
    storage_root: str,
    lookback: str | None = None,
    train_matrix_artifact_id: str | None = None,
    source_pins: Mapping[str, str | None] | None = None,
    policy: str = "exact",
) -> MatrixResult:
    """Assemble (or reuse) a Parquet design matrix and register it in the artifact DAG.

    The seam (ADR-0008) and the imputation leakage boundary (ADR-0009): featurizer renders a
    dense feature matrix over ``as_of_dates`` with the *exclusive* point-in-time boundary;
    the cohort + labels select it down by inner join; fit-based imputation statistics are
    fitted on the *train* matrix only and reused for *test*.

    Args:
        db_engine: project-database engine (greenfield ``triage.*`` schema).
        run_id: the owning run; must already exist (FK).
        featurizer_config: the featurizer ER-graph config (dict). ``as_of_boundary`` is
            forced to ``exclusive``.
        cohort_artifact_id: the built cohort (selection mask + feature-group/matrix parent).
        labels_artifact_id: the built labels (the matrix's target join).
        temporal_config: validated :class:`TemporalConfig`; its ``canonical()`` enters the
            matrix node's identity.
        imputation_policy: :class:`ImputationPolicy`; ``canonical()`` enters identity, and
            its fit-based rules drive the train-only statistics.
        matrix_kind: ``'train'`` | ``'test'`` (a :class:`triage.split_kind`).
        as_of_dates: the split's ``as_of_times`` (from timechop via ``temporal_config``).
        label_timespan: the split's label horizon (e.g. ``'6 months'``); joins labels and is
            written to ``matrices.label_timespan``.
        storage: the :class:`~triage.profiles.protocols.StorageAdapter` the Parquet matrix is
            written/read through (local FS or S3).
        storage_root: the artifact root URI; the matrix lands at
            ``<storage_root>/<uuid>.parquet``.
        lookback: optional ``max_training_history`` for train matrices (``matrices.lookback``).
        train_matrix_artifact_id: ``None`` for a train matrix; the train matrix's artifact_id
            for a test matrix — the leakage-boundary parent that carries the fitted stats.
        source_pins: declared-source → version pins (volatile if unpinned, ADR-0014).
        policy: cache lookup policy ('exact' default).

    Returns:
        A :class:`MatrixResult` (the matrix + feature-group ids, storage uri, shape, the
        fitted stats, and whether this was a cache hit).
    """
    if matrix_kind not in ("train", "test", "validation", "production"):
        raise ValueError(
            f"matrix_kind {matrix_kind!r} is not a triage.split_kind"
            + " ('train'|'test'|'validation'|'production')"
        )
    if matrix_kind in ("test", "production") and train_matrix_artifact_id is None:
        raise ValueError(
            f"a {matrix_kind} matrix requires train_matrix_artifact_id — the train-fitted"
            + " imputation statistics flow along that parent edge, so test/production reuse"
            + " the train fit rather than refitting (ADR-0009, adapter-spec §3.6)"
        )
    if matrix_kind == "train" and train_matrix_artifact_id is not None:
        raise ValueError("a train matrix must not take a train_matrix parent")
    if not as_of_dates:
        raise ValueError("build_matrix requires at least one as_of_date")

    cohort_deriv = _reconstruct_derivation(db_engine, cohort_artifact_id, "cohort")
    labels_deriv = _reconstruct_derivation(db_engine, labels_artifact_id, "labels")

    # ---- feature_group node: canonical featurizer config + cohort parent + featurizer ver.
    fg_config = {"featurizer": _canonical_featurizer(featurizer_config)}
    fg_derivation = derive(
        kind=FEATURE_GROUP_KIND,
        config=fg_config,
        parents=[cohort_deriv],
        source_pins=source_pins,
        engine_versions=engine_versions_for(FEATURE_GROUP_KIND),
    )

    # ---- matrix node: temporal + feature-group + imputation policy + kind + timespan
    # + this matrix's own as_of_dates + lookback. The split's as_of_dates and lookback
    # determine the matrix's rows (each split inner-joins its OWN dates against the shared
    # cohort/labels), so two splits sharing one global temporal_config still produce
    # distinct matrices — they must hash to distinct artifact ids, else a later split's
    # matrix would cache-hit an earlier split's content (a different row set). Note this
    # is the matrix's *own* date slice entering its *own* identity — unlike the cohort and
    # labels, which deliberately span ALL split dates and never fold per-split dates in.
    matrix_config = {
        "temporal_config": temporal_config.canonical(),
        "feature_group": fg_config["featurizer"],
        "imputation_policy": imputation_policy.canonical(),
        "matrix_kind": matrix_kind,
        "label_timespan": label_timespan,
        "as_of_dates": sorted(d.isoformat() for d in as_of_dates),
        "lookback": lookback,
    }
    matrix_parent_derivs = [fg_derivation, cohort_deriv, labels_deriv]
    if train_matrix_artifact_id is not None:
        matrix_parent_derivs.append(
            _reconstruct_derivation(db_engine, train_matrix_artifact_id, "train matrix")
        )
    matrix_derivation = derive(
        kind=MATRIX_KIND,
        config=matrix_config,
        parents=matrix_parent_derivs,
        source_pins=source_pins,
        engine_versions=engine_versions_for(MATRIX_KIND),
    )

    # ---- cache: an already-built matrix is reused wholesale (its feature group too).
    # status='built' alone is NOT proof of presence (ADR-0017: outputs are deletable —
    # GC, or an OS tmp purge, observed live): verify the Parquet exists, else rebuild.
    hit = cache_hit(db_engine, matrix_derivation, policy=policy)
    if hit is not None:
        existing = _existing_matrix_row(db_engine, matrix_derivation.id)
        if existing is not None and storage.exists(existing["storage_uri"]):
            logger.info(
                f"Matrix {matrix_derivation.id[:12]}… ({matrix_kind}) already built — reusing"
            )
            record_use(db_engine, run_id, [fg_derivation.id, matrix_derivation.id])
            return MatrixResult(
                matrix_artifact_id=matrix_derivation.id,
                feature_group_artifact_id=fg_derivation.id,
                storage_uri=existing["storage_uri"],
                num_entities=existing["num_entities"],
                num_features=existing["num_features"],
                feature_names=list(existing["feature_names"] or []),
                fit_based_stats=(existing["metadata"] or {}).get(_FIT_STATS_KEY, {}),
                cat_encodings=(existing["metadata"] or {}).get(_CAT_ENC_KEY, {}),
                cache_hit=True,
            )
        logger.warning(
            f"Matrix {matrix_derivation.id[:12]}… is marked built but its Parquet is"
            f" missing ({(existing or {}).get('storage_uri', '?')}) — rebuilding under"
            " the same identity"
        )

    # ---- feature group artifact (built first; the matrix lists it as a parent).
    fg_built = cache_hit(db_engine, fg_derivation, policy=policy) is not None
    if not fg_built:
        begin_artifact(
            db_engine,
            fg_derivation,
            kind=FEATURE_GROUP_KIND,
            config=fg_config,
            source_pins=source_pins,
            engine_versions=engine_versions_for(FEATURE_GROUP_KIND),
            run_id=run_id,
            parents=[cohort_artifact_id],
        )

    begin_artifact(
        db_engine,
        matrix_derivation,
        kind=MATRIX_KIND,
        config=matrix_config,
        source_pins=source_pins,
        engine_versions=engine_versions_for(MATRIX_KIND),
        run_id=run_id,
        parents=[
            fg_derivation.id,
            cohort_artifact_id,
            labels_artifact_id,
            *([train_matrix_artifact_id] if train_matrix_artifact_id else []),
        ],
    )

    try:
        result = _assemble(
            db_engine=db_engine,
            featurizer_config=featurizer_config,
            cohort_artifact_id=cohort_artifact_id,
            labels_artifact_id=labels_artifact_id,
            imputation_policy=imputation_policy,
            matrix_kind=matrix_kind,
            as_of_dates=as_of_dates,
            label_timespan=label_timespan,
            train_matrix_artifact_id=train_matrix_artifact_id,
            matrix_artifact_id=matrix_derivation.id,
            storage=storage,
            storage_root=storage_root,
        )
        if not fg_built:
            mark_built(
                db_engine,
                fg_derivation.id,
                output_ref="featurizer:feature_group",
                kind=FEATURE_GROUP_KIND,
                run_id=run_id,
            )
        _insert_matrix_row(
            db_engine,
            matrix_artifact_id=matrix_derivation.id,
            matrix_kind=matrix_kind,
            result=result,
            label_timespan=label_timespan,
            lookback=lookback,
            run_id=run_id,
        )
        mark_built(
            db_engine,
            matrix_derivation.id,
            output_ref=result.storage_uri,
            kind=MATRIX_KIND,
            run_id=run_id,
        )
    except Exception:
        mark_failed(db_engine, matrix_derivation.id, kind=MATRIX_KIND, run_id=run_id)
        if not fg_built:
            mark_failed(
                db_engine, fg_derivation.id, kind=FEATURE_GROUP_KIND, run_id=run_id
            )
        raise

    record_use(db_engine, run_id, [fg_derivation.id, matrix_derivation.id])
    logger.info(
        f"Built {matrix_kind} matrix {matrix_derivation.id[:12]}… —"
        + f" {result.num_entities} rows × {result.num_features} features -> {result.storage_uri}"
    )
    return MatrixResult(
        matrix_artifact_id=matrix_derivation.id,
        feature_group_artifact_id=fg_derivation.id,
        storage_uri=result.storage_uri,
        num_entities=result.num_entities,
        num_features=result.num_features,
        feature_names=result.feature_names,
        fit_based_stats=result.fit_based_stats,
        cat_encodings=result.cat_encodings,
        cache_hit=False,
    )


def _canonical_featurizer(featurizer_config: Mapping[str, Any]) -> dict[str, Any]:
    """The featurizer config slice that enters identity, with the boundary normalized.

    The boundary is forced to ``exclusive`` (same as the rendered config) so the hash is
    stable regardless of whether the caller supplied it.
    """
    cfg = dict(featurizer_config)
    cfg["as_of_boundary"] = "exclusive"
    return cfg


def _assemble(
    db_engine: DictRowPool,
    featurizer_config: Mapping[str, Any],
    cohort_artifact_id: str,
    labels_artifact_id: str,
    imputation_policy: ImputationPolicy,
    matrix_kind: str,
    as_of_dates: Sequence[date],
    label_timespan: str,
    train_matrix_artifact_id: str | None,
    matrix_artifact_id: str,
    storage: StorageAdapter,
    storage_root: str,
) -> MatrixResult:
    """The assembly itself: features ⋈ cohort ⋈ labels, impute, write Parquet."""
    import polars as pl

    feature_table, target_id_col = _run_featurizer(
        db_engine, featurizer_config, as_of_dates
    )
    features = pl.from_arrow(feature_table)
    assert isinstance(features, pl.DataFrame)  # from_arrow(Table) is a frame
    # Normalize the target id to triage's universal ``entity_id`` for joining.
    if target_id_col != "entity_id":
        # An event-grain target (e.g. the visit-level regime: id = event_id) can carry a
        # passthrough column literally named entity_id — its relationship key to the
        # parent entity. It is a join key, never a feature; drop it or the rename below
        # duplicates the universal id column.
        if "entity_id" in features.columns:
            features = features.drop("entity_id")
        features = features.rename({target_id_col: "entity_id"})
    features = features.with_columns(pl.col(_AS_OF_COL).cast(pl.Date))

    cohort = _load_cohort(db_engine, cohort_artifact_id)
    labels = _load_labels(db_engine, labels_artifact_id, label_timespan)

    # cohort is the selection mask (inner join); labels carry the target (left join keeps a
    # cohort row whose label is unknown — a NULL outcome — rather than dropping it).
    design = features.join(cohort, on=["entity_id", _AS_OF_COL], how="inner").join(
        labels, on=["entity_id", _AS_OF_COL], how="left"
    )

    feature_columns = _feature_columns(features.columns, "entity_id")

    # Categorical encoding FIRST — turn direct-categorical *strings* into ordinal codes
    # (train-fit, reused for test) so the matrix Parquet is fully numeric for sklearn
    # (adapter-spec §4). Column names are unchanged (ordinal), so feature_columns still holds.
    cat_encodings = _resolve_cat_encodings(
        db_engine=db_engine,
        design=design,
        feature_columns=feature_columns,
        train_matrix_artifact_id=train_matrix_artifact_id,
    )
    design = _apply_cat_encoding(design, cat_encodings)

    # Drop any feature column still non-numeric after categorical encoding — the target entity's
    # temporal_ix leaks through featurizer as a Date-typed *feature*, and sklearn needs a numeric
    # design matrix (model._design_X does frame.select(feature_names).to_numpy(), no dtype
    # filter). Post cat-encoding every legitimate feature is numeric, so a residual non-numeric
    # column is a leak: drop it from BOTH the feature set and the written Parquet. Keys/labels
    # are never in feature_columns, so they are untouched.
    numeric_feature_columns = _numeric_feature_columns(design, feature_columns)
    leaked = [c for c in feature_columns if c not in numeric_feature_columns]
    if leaked:
        design = design.drop(leaked)
    feature_columns = numeric_feature_columns

    fitted = _resolve_fit_stats(
        db_engine=db_engine,
        design=design,
        feature_columns=feature_columns,
        imputation_policy=imputation_policy,
        matrix_kind=matrix_kind,
        train_matrix_artifact_id=train_matrix_artifact_id,
    )
    design = _apply_fit_based(design, feature_columns, fitted)
    # Apply the policy's fit-free fills (zero/constant) on top of featurizer's narrower
    # count-like zero-fill — closes the recency/tenure NULL gap (ADR-0009). Leakage-safe.
    design = _apply_fit_free(design, feature_columns, imputation_policy)
    _check_no_nulls_remain(design, feature_columns, imputation_policy)

    storage_uri = storage.join(storage_root, f"{as_uuid(matrix_artifact_id)}.parquet")
    write_parquet(storage, storage_uri, design)

    return MatrixResult(
        matrix_artifact_id=matrix_artifact_id,
        feature_group_artifact_id="",  # filled by the caller
        storage_uri=storage_uri,
        num_entities=design.height,
        num_features=len(feature_columns),
        feature_names=feature_columns,
        fit_based_stats=fitted,
        cat_encodings=cat_encodings,
        cache_hit=False,
    )


def _resolve_fit_stats(
    db_engine: DictRowPool,
    design,
    feature_columns: Sequence[str],
    imputation_policy: ImputationPolicy,
    matrix_kind: str,
    train_matrix_artifact_id: str | None,
) -> dict[str, dict[str, Any]]:
    """The fitted statistics for this matrix — the leakage boundary in one place.

    * **Train matrix**: compute each fit-based feature's statistic over *these* rows (the
      train split) and return them for persistence.
    * **Test matrix**: do **not** compute — read the train matrix's persisted stats from its
      ``triage.matrices.metadata`` and reuse them verbatim. Recomputing here would refit on
      the test split — exactly the ADR-0009 leak.
    """
    if not imputation_policy.requires_fit():
        return {}

    if train_matrix_artifact_id is not None:
        train_meta = _existing_matrix_row(db_engine, train_matrix_artifact_id)
        stats = (train_meta["metadata"] or {}).get(_FIT_STATS_KEY, {})
        logger.info(
            f"Test matrix reusing {len(stats)} train-fitted imputation stat(s)"
            + " (no refit on test — ADR-0009 leakage boundary)"
        )
        return dict(stats)

    fitted: dict[str, dict[str, Any]] = {}
    for feature in feature_columns:
        rule = imputation_policy.resolve(_metric_of(feature))
        if not rule.fits_on_train:
            continue
        value = _fit_statistic(design, feature, rule)
        fitted[feature] = {"stat": rule.type, "value": value}
    logger.info(f"Train matrix fitted {len(fitted)} imputation statistic(s)")
    return fitted


def _load_cohort(db_engine: DictRowPool, cohort_artifact_id: str):
    """The cohort selection mask as a Polars frame: (entity_id, as_of_date)."""
    import polars as pl

    with db_engine.connection() as conn:
        rows = conn.execute(
            "select entity_id, as_of_date from triage.cohorts"
            + " where cohort_hash = %(h)s",
            {"h": cohort_artifact_id},
        ).fetchall()
    return pl.DataFrame(
        {
            "entity_id": [r["entity_id"] for r in rows],
            _AS_OF_COL: [r["as_of_date"] for r in rows],
        },
        schema={"entity_id": pl.Int64, _AS_OF_COL: pl.Date},
    )


def _load_labels(db_engine: DictRowPool, labels_artifact_id: str, label_timespan: str):
    """The labels target as a Polars frame, filtered to this split's label_timespan.

    Carries all of ``outcome``/``duration``/``event_observed`` — the unused columns are NULL
    per the problem_type routing (ADR-0010) and harmless to join through.
    """
    import polars as pl

    with db_engine.connection() as conn:
        rows = conn.execute(
            "select entity_id, as_of_date, outcome, duration, event_observed"
            + " from triage.labels"
            + " where label_hash = %(h)s and label_timespan = cast(%(ts)s as interval)",
            {"h": labels_artifact_id, "ts": label_timespan},
        ).fetchall()
    return pl.DataFrame(
        {
            "entity_id": [r["entity_id"] for r in rows],
            _AS_OF_COL: [r["as_of_date"] for r in rows],
            "outcome": [r["outcome"] for r in rows],
            "duration": [r["duration"] for r in rows],
            "event_observed": [r["event_observed"] for r in rows],
        },
        schema={
            "entity_id": pl.Int64,
            _AS_OF_COL: pl.Date,
            "outcome": pl.Float64,
            "duration": pl.Float64,
            "event_observed": pl.Boolean,
        },
    )


def _existing_matrix_row(
    db_engine: DictRowPool, matrix_artifact_id: str
) -> dict[str, Any]:
    with db_engine.connection() as conn:
        row = conn.execute(
            "select storage_uri, num_entities, num_features, feature_names,"
            + " metadata from triage.matrices where matrix_uuid = %(u)s",
            {"u": as_uuid(matrix_artifact_id)},
        ).fetchone()
    if row is None:
        raise ValueError(
            f"matrix artifact {matrix_artifact_id!r} has no triage.matrices row —"
            + " was it built? (a test matrix needs its train parent's stats)"
        )
    return dict(row)


def _insert_matrix_row(
    db_engine: DictRowPool,
    matrix_artifact_id: str,
    matrix_kind: str,
    result: MatrixResult,
    label_timespan: str,
    lookback: str | None,
    run_id: str,
) -> None:
    metadata = {
        _FIT_STATS_KEY: result.fit_based_stats,
        _CAT_ENC_KEY: result.cat_encodings,
    }
    with db_engine.connection() as conn:
        conn.execute(
            "insert into triage.matrices"
            + " (matrix_uuid, artifact_id, matrix_kind, storage_uri, storage_format,"
            + "  num_entities, num_features, feature_names, label_timespan, lookback,"
            + "  metadata, built_by_run)"
            + " values (%(uuid)s, %(artifact_id)s, cast(%(kind)s as triage.split_kind),"
            + "  %(storage_uri)s, 'parquet', %(num_entities)s, %(num_features)s, %(feature_names)s,"
            + "  cast(%(label_timespan)s as interval), cast(%(lookback)s as interval),"
            + "  cast(%(metadata)s as jsonb), %(run_id)s)"
            + " on conflict (matrix_uuid) do update set"
            + "  storage_uri = excluded.storage_uri,"
            + "  num_entities = excluded.num_entities,"
            + "  num_features = excluded.num_features,"
            + "  feature_names = excluded.feature_names,"
            + "  metadata = excluded.metadata",
            {
                "uuid": as_uuid(matrix_artifact_id),
                "artifact_id": matrix_artifact_id,
                "kind": matrix_kind,
                "storage_uri": result.storage_uri,
                "num_entities": result.num_entities,
                "num_features": result.num_features,
                "feature_names": result.feature_names,
                "label_timespan": label_timespan,
                "lookback": lookback,
                "metadata": _json(metadata),
                "run_id": run_id,
            },
        )


def _json(value: Any) -> str:
    import json

    return json.dumps(value)
