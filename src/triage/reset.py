"""Reset a project's triage results while keeping its configuration.

``triage reset`` exists for the moment a data version changes under a project: every
experiment, run, model, matrix, prediction and evaluation built on the old data is wrong
and has to go, but the schema, the source registry and the Alembic stamp stay. It works
the same in a per-project database and in a host database that carries the ``triage``
schema next to its own (where ``triage project drop`` does not apply).

Which tables are results and which are configuration was decided from the migrations:

* **Configuration, never touched** — :data:`CONFIG_TABLES`: the source registry
  (``sources``, ``source_versions``), user-registered metric directions and the Alembic
  version table.
* **Results** — :data:`RESULT_TABLES`, in FK-safe delete order. Every row hangs off a run,
  an experiment, an artifact or a model.
* **Run-time inputs without run keys** — :data:`SHARED_TABLES`: ``protected_groups`` is
  filled by the ``bias_config`` query over the project's data at run time, and
  ``subsets``/``subset_members`` by the ``subsets`` queries. They are keyed by entity and
  date, not by run, and every experiment shares them, so only ``--all`` clears them. They
  are rebuilt idempotently by the next run.

A scoped reset (``before`` or ``experiments``) removes the selected runs and experiments
and every artifact they built or used that no surviving run still reaches through its
upstream closure — the same liveness rule as ``triage gc``, rooted on the surviving runs.
Everything happens in one transaction; file-backed outputs (matrices, models) are deleted
after the commit through the storage layer ``triage gc --delete`` uses.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, LiteralString

from psycopg import Connection, sql
from psycopg.rows import DictRow

from triage.artifacts import FEATURE_GROUP_OUTPUT_REF
from triage.logging import get_logger
from triage.util.db import DictRowPool, returned_row

logger = get_logger(__name__)

CONFIG_TABLES: tuple[str, ...] = (
    "sources",
    "source_versions",
    "metric_directions",
    "results_schema_versions",
)

SHARED_TABLES: tuple[str, ...] = ("protected_groups", "subset_members", "subsets")

_BY_MODEL: LiteralString = "model_id in (select model_id from reset_models)"
_BY_ARTIFACT_ID: LiteralString = (
    "artifact_id in (select artifact_id from reset_artifacts)"
)
_BY_RUN: LiteralString = "run_id in (select run_id from reset_runs)"

# (table, scoped predicate), children before parents. The predicates read the temp
# tables built by _select_scope, which are fixed before the first delete runs.
_SCOPED_DELETES: tuple[tuple[str, LiteralString], ...] = (
    (
        "predictions",
        _BY_MODEL + " or matrix_uuid in (select matrix_uuid from reset_matrices)",
    ),
    ("evaluations", _BY_MODEL),
    ("bias_metrics", _BY_MODEL),
    ("crosstabs", _BY_MODEL),
    ("error_analysis", _BY_MODEL),
    ("error_tree_fits", _BY_MODEL),
    ("feature_importances", _BY_MODEL),
    ("individual_importances", _BY_MODEL),
    ("models", _BY_MODEL),
    (
        "model_groups",
        "model_group_id in (select model_group_id from reset_model_groups)",
    ),
    ("matrices", "matrix_uuid in (select matrix_uuid from reset_matrices)"),
    ("cohorts", "cohort_hash in (select artifact_id from reset_artifacts)"),
    ("labels", "label_hash in (select artifact_id from reset_artifacts)"),
    ("run_artifacts", _BY_RUN + " or " + _BY_ARTIFACT_ID),
    ("artifact_inputs", _BY_ARTIFACT_ID),
    ("artifacts", _BY_ARTIFACT_ID),
    ("run_source_pins", _BY_RUN),
    ("runs", _BY_RUN),
    (
        "experiments",
        "experiment_hash in (select experiment_hash from reset_experiments)",
    ),
)

RESULT_TABLES: tuple[str, ...] = tuple(table for table, _ in _SCOPED_DELETES)

# Artifacts a scoped reset may delete: those its runs used or built, minus everything a
# surviving run or a surviving artifact still reaches upstream (ADR-0017's liveness rule).
_SCOPED_ARTIFACTS_SQL = """
create temp table reset_artifacts on commit drop as
with recursive candidates as (
    select artifact_id from triage.run_artifacts
     where run_id in (select run_id from reset_runs)
    union
    select artifact_id from triage.artifacts
     where built_by_run in (select run_id from reset_runs)
),
roots as (
    select a.artifact_id from triage.artifacts a
     where a.artifact_id not in (select artifact_id from candidates)
    union
    select ra.artifact_id from triage.run_artifacts ra
     where ra.run_id not in (select run_id from reset_runs)
),
live as (
    select artifact_id from roots
    union
    select i.parent_id
      from live l
      join triage.artifact_inputs i on i.artifact_id = l.artifact_id
)
select artifact_id from candidates
except
select artifact_id from live
"""


class ResetError(RuntimeError):
    """The reset cannot run as asked; the message says what to do instead."""


@dataclass(frozen=True)
class ResetScope:
    """Which results to remove. Exactly one of the three must be set.

    Attributes:
        all: Every result, plus the run-time inputs in :data:`SHARED_TABLES`.
        before: Runs started before this instant, and experiments created before it
            that keep no surviving run. A naive datetime is read in the database
            session's time zone.
        experiments: Full experiment hashes; their runs go with them.
    """

    all: bool = False
    before: datetime | None = None
    experiments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        chosen = sum([self.all, self.before is not None, bool(self.experiments)])
        if chosen != 1:
            raise ResetError(
                "Choose exactly one scope: --all, --before <timestamp>, or"
                + " --experiment <hash> (repeatable)."
            )

    def as_dict(self) -> dict[str, Any]:
        """The scope as JSON-ready data."""
        if self.all:
            return {"all": True}
        if self.before is not None:
            return {"before": self.before.isoformat()}
        return {"experiments": list(self.experiments)}


@dataclass
class ResetResult:
    """What a reset deleted, or would delete on a dry run.

    Attributes:
        database: ``current_database()`` of the connection.
        scope: The scope that ran.
        dry_run: True when nothing was deleted.
        tables: Rows per table, in delete order.
        kept: Tables the reset does not touch.
        external: The file-backed outputs (``output_ref``) of the deleted artifacts.
        deleted: Refs the storage layer deleted.
        absent: Refs that were already gone.
        failed: ``{ref, error}`` for every ref the storage layer could not delete.
    """

    database: str
    scope: ResetScope
    dry_run: bool
    tables: dict[str, int] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        """Rows over every table."""
        return sum(self.tables.values())

    def as_dict(self) -> dict[str, Any]:
        """The result as JSON-ready data."""
        return {
            "database": self.database,
            "scope": self.scope.as_dict(),
            "dry_run": self.dry_run,
            "tables": self.tables,
            "total_rows": self.total_rows,
            "kept": self.kept,
            "external": {
                "refs": self.external,
                "deleted": self.deleted,
                "absent": self.absent,
                "failed": self.failed,
            },
        }


def _require_tables(conn: Connection[DictRow]) -> None:
    expected = (*RESULT_TABLES, *SHARED_TABLES, *CONFIG_TABLES)
    missing = [
        table
        for table in expected
        if returned_row(
            conn.execute(
                "select to_regclass(%(t)s) is null as missing",
                {"t": f"triage.{table}"},
            ).fetchone()
        )["missing"]
    ]
    if missing:
        raise ResetError(
            f"The triage schema in this database lacks {', '.join(missing)} — run"
            + " `triage db upgrade` first, then reset."
        )


def _select_scope(conn: Connection[DictRow], scope: ResetScope) -> None:
    """Fill the reset_* temp tables that every predicate reads."""
    params: dict[str, Any] = {
        "before": scope.before,
        "hashes": list(scope.experiments),
    }
    run_where: LiteralString
    experiment_where: LiteralString
    if scope.all:
        run_where, experiment_where = "true", "true"
    elif scope.before is not None:
        run_where = "started_at < %(before)s"
        experiment_where = (
            "created_at < %(before)s and not exists ("
            " select 1 from triage.runs r"
            " where r.experiment_hash = e.experiment_hash"
            " and r.run_id not in (select run_id from reset_runs))"
        )
    else:
        run_where = "experiment_hash = any(%(hashes)s)"
        experiment_where = "experiment_hash = any(%(hashes)s)"

    conn.execute(
        "create temp table reset_runs on commit drop as"
        + f" select run_id from triage.runs where {run_where}",
        params,
    )
    conn.execute(
        "create temp table reset_experiments on commit drop as"
        + f" select experiment_hash from triage.experiments e where {experiment_where}",
        params,
    )
    if scope.all:
        conn.execute(
            "create temp table reset_artifacts on commit drop as"
            + " select artifact_id from triage.artifacts"
        )
    else:
        conn.execute(_SCOPED_ARTIFACTS_SQL)
    conn.execute(
        "create temp table reset_models on commit drop as"
        + " select model_id, model_group_id from triage.models"
        + " where model_hash in (select artifact_id from reset_artifacts)"
    )
    conn.execute(
        "create temp table reset_matrices on commit drop as"
        + " select matrix_uuid from triage.matrices"
        + " where artifact_id in (select artifact_id from reset_artifacts)"
    )
    # A model group goes only when none of its models survives.
    conn.execute(
        "create temp table reset_model_groups on commit drop as"
        + " select distinct rm.model_group_id from reset_models rm"
        + " where not exists (select 1 from triage.models m"
        + "   where m.model_group_id = rm.model_group_id"
        + "   and m.model_id not in (select model_id from reset_models))"
    )


def _table(name: str) -> sql.Identifier:
    return sql.Identifier("triage", name)


def _count(conn: Connection[DictRow], table: str, where: LiteralString = "true") -> int:
    query = sql.SQL("select count(*) as n from {} where {}").format(
        _table(table), sql.SQL(where)
    )
    return int(returned_row(conn.execute(query).fetchone())["n"])


def _external_refs(conn: Connection[DictRow]) -> list[str]:
    """File-backed outputs of the doomed artifacts; in-PG and virtual ones excluded."""
    rows = conn.execute(
        """
        select output_ref from triage.artifacts
         where artifact_id in (select artifact_id from reset_artifacts)
           and output_ref is not null
           and kind not in ('cohort', 'labels')
           and output_ref <> %(virtual)s
         order by kind, output_ref
        """,
        {"virtual": FEATURE_GROUP_OUTPUT_REF},
    ).fetchall()
    return [row["output_ref"] for row in rows]


def _refresh_leaderboard(conn: Connection[DictRow]) -> None:
    found = returned_row(
        conn.execute(
            "select to_regclass('triage.leaderboard') is not null as e"
        ).fetchone()
    )
    if found["e"]:
        conn.execute("refresh materialized view triage.leaderboard")


def reset_results(
    pool: DictRowPool, scope: ResetScope, *, dry_run: bool = True
) -> ResetResult:
    """Delete the results in ``scope`` in one transaction, or count them on a dry run.

    Args:
        pool: The project (or host) database pool.
        scope: Which results to remove.
        dry_run: Count only and roll back. The default.

    Returns:
        The per-table counts and the external refs. On a real run the external refs
        are not deleted yet — call :func:`delete_external` after this returns, so a
        failed file delete never rolls back the committed reset.

    Raises:
        ResetError: The schema is not at a version this command knows, or an
            experiment hash does not exist.
    """
    with pool.connection() as conn:
        _require_tables(conn)
        database = str(
            returned_row(conn.execute("select current_database() as d").fetchone())["d"]
        )
        if scope.experiments:
            found = {
                row["experiment_hash"]
                for row in conn.execute(
                    "select experiment_hash from triage.experiments"
                    + " where experiment_hash = any(%(h)s)",
                    {"h": list(scope.experiments)},
                ).fetchall()
            }
            unknown = sorted(set(scope.experiments) - found)
            if unknown:
                raise ResetError(f"No such experiment: {', '.join(unknown)}")

        tables = (*RESULT_TABLES, *SHARED_TABLES) if scope.all else RESULT_TABLES
        kept = list(CONFIG_TABLES) if scope.all else [*SHARED_TABLES, *CONFIG_TABLES]
        if scope.all and not dry_run:
            # TRUNCATE takes these locks anyway; taking them first makes the counts exact.
            conn.execute(
                sql.SQL("lock table {} in access exclusive mode").format(
                    sql.SQL(", ").join(_table(t) for t in tables)
                )
            )
        _select_scope(conn, scope)
        external = _external_refs(conn)

        counts: dict[str, int] = {}
        if scope.all:
            for table in tables:
                counts[table] = _count(conn, table)
        else:
            for table, where in _SCOPED_DELETES:
                counts[table] = _count(conn, table, where)

        result = ResetResult(
            database=database,
            scope=scope,
            dry_run=dry_run,
            tables=counts,
            kept=kept,
            external=external,
        )
        if dry_run:
            conn.rollback()
            return result

        if scope.all:
            conn.execute(
                sql.SQL("truncate table {}").format(
                    sql.SQL(", ").join(_table(t) for t in tables)
                )
            )
        else:
            for table, where in _SCOPED_DELETES:
                counts[table] = conn.execute(
                    sql.SQL("delete from {} where {}").format(
                        _table(table), sql.SQL(where)
                    )
                ).rowcount
        _refresh_leaderboard(conn)
    logger.info(
        f"Reset {database}: {result.total_rows} row(s) over {len(counts)} table(s);"
        + f" {len(external)} file-backed output(s) to delete"
    )
    return result


def delete_external(result: ResetResult, refs: Sequence[str] | None = None) -> None:
    """Delete the file-backed outputs of a committed reset through the storage layer.

    Each ref is tried on its own. A failure is logged with its ref and recorded in
    ``result.failed``; it never stops the other deletes and never undoes the reset.

    Args:
        result: The result of a non-dry :func:`reset_results`; updated in place.
        refs: Refs to delete instead of ``result.external`` (tests).
    """
    from triage.profiles.storage import storage_for_root

    for ref in result.external if refs is None else refs:
        try:
            if storage_for_root(ref).delete(ref):
                result.deleted.append(ref)
            else:
                result.absent.append(ref)
        except Exception as exc:  # each ref reported; the reset is already committed
            logger.error(f"Could not delete reset output {ref}: {exc!r}")
            result.failed.append({"ref": ref, "error": f"{type(exc).__name__}: {exc}"})
