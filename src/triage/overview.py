"""What an editor front end reads: a Project in one call, an Experiment in another.

``triage status --json --full`` and ``triage experiment show`` print the payloads built here.
Emacs (``emacs/triage.el``) starts one of these processes per refresh, because each CLI
process costs about a second to start, and draws the JSON that comes back. Nothing here
computes a number of its own: the status is the lynkeus ``Status`` the TUI renders, the
identity is :func:`~triage.adapters.run.experiment_hash_for`, the plan is
:func:`~triage.adapters.preflight.plan_experiment`, and the history rows come from the readers
the ``leaderboard``, ``models`` and ``runs list`` verbs print.

The payloads are a public interface. Their shape is pinned by the fixtures under
``emacs/test/fixtures/`` (``src/tests/overview_tests/test_emacs_fixtures.py``), and
:data:`CONTRACT` is bumped on any change that would break a reader of the old shape.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Optional

import yaml
from lynkeus.models import jsonable

from triage.logging import get_logger
from triage.util.db import DictRowPool, connection_pool

logger = get_logger(__name__)

__all__ = [
    "CONTRACT",
    "DEFAULT_CONFIG_GLOBS",
    "MAX_CONFIG_FILES",
    "ConfigEntry",
    "TargetError",
    "discover_configs",
    "experiment_object",
    "full_status",
    "match_experiment_hashes",
]

#: The version of the JSON contract an editor front end reads. Bump it on any rename,
#: removal or type change in the ``status --full`` or ``experiment show`` payloads.
CONTRACT = 1

#: Where config discovery looks by default: every YAML file under the project root.
DEFAULT_CONFIG_GLOBS: tuple[str, ...] = ("**/*.yaml", "**/*.yml")

#: Directories config discovery never enters, besides any whose name starts with a dot.
SKIPPED_DIRS = frozenset(
    {".venv", "node_modules", "dbt_packages", "target", "docs-site"}
)

#: Discovery stops after this many YAML files and says so in ``warnings``.
MAX_CONFIG_FILES = 500

#: Two top-level keys every experiment config has and other YAML files (dbt, compose,
#: ``database.yaml``) do not.
_CONFIG_KEYS = ("temporal_config", "label_config")


class TargetError(ValueError):
    """``experiment show`` was given a TARGET that is neither a file nor one known hash."""


# ------------------------------------------------------------------ config discovery


@dataclass(frozen=True)
class ConfigEntry:
    """One experiment config found under the project root.

    ``experiment_hash`` is the Experiment the file belongs to, so the file joins to the
    database on it. ``error`` is set, and the other fields may be ``None``, when the file
    looks like a config but cannot be parsed or hashed.
    """

    path: str
    experiment_hash: Optional[str]
    name: Optional[str]
    problem_type: Optional[str]
    error: Optional[str] = None


def _skipped(dirname: str) -> bool:
    return dirname.startswith(".") or dirname in SKIPPED_DIRS


def _matches(relpath: str, globs: Sequence[str]) -> bool:
    """fnmatch over the root-relative POSIX path; a leading ``**/`` also matches the root.

    ``*`` crosses ``/`` in fnmatch, so ``**/*.yaml`` reads as "any .yaml below the root" and
    the ``**/``-stripped form adds the files directly in it.
    """
    return any(
        fnmatchcase(relpath, glob)
        or (glob.startswith("**/") and fnmatchcase(relpath, glob[3:]))
        for glob in globs
    )


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _config_entry(root: Path, relpath: str) -> Optional[ConfigEntry]:
    """The entry for one YAML file, or ``None`` when the file is not an experiment config."""
    from triage.adapters.run import experiment_hash_for

    try:
        text = (root / relpath).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ConfigEntry(relpath, None, None, None, f"cannot read: {_one_line(exc)}")
    try:
        data = yaml.full_load(text)
    except yaml.YAMLError as exc:
        # A broken file is listed only when it names both config keys: a templated YAML
        # elsewhere in the tree is not something the user asked to see.
        if all(key in text for key in _CONFIG_KEYS):
            return ConfigEntry(relpath, None, None, None, _one_line(exc))
        return None
    if not isinstance(data, Mapping) or not all(key in data for key in _CONFIG_KEYS):
        return None
    name, problem_type = (
        None if data.get(key) is None else str(data[key])
        for key in ("name", "problem_type")
    )
    try:
        experiment_hash = experiment_hash_for(data)
    except (TypeError, ValueError) as exc:
        return ConfigEntry(
            relpath, None, name, problem_type, f"cannot hash: {_one_line(exc)}"
        )
    return ConfigEntry(relpath, experiment_hash, name, problem_type)


def discover_configs(
    root: Path,
    globs: Sequence[str] = DEFAULT_CONFIG_GLOBS,
    *,
    limit: int = MAX_CONFIG_FILES,
) -> tuple[list[ConfigEntry], list[str]]:
    """Find the experiment configs under ``root`` and hash each one.

    A file counts as a config when it parses as a YAML mapping with ``temporal_config`` and
    ``label_config`` keys. Configs are hashed, not validated: validation loads featurizer's
    planner, which costs time for every file, so it belongs to ``experiment show``.

    Args:
        root: the project root; paths in the result are relative to it.
        globs: fnmatch patterns over the root-relative POSIX path.
        limit: stop after this many YAML files.

    Returns:
        ``(entries, warnings)``, entries in path order. ``warnings`` says when the limit
        stopped the search.
    """
    entries: list[ConfigEntry] = []
    examined = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skipped(d))
        here = Path(dirpath)
        for filename in sorted(filenames):
            relpath = (here / filename).relative_to(root).as_posix()
            if filename.startswith(".") or not _matches(relpath, globs):
                continue
            if examined >= limit:
                return entries, [
                    f"config discovery stopped after {limit} YAML files under {root};"
                    " narrow it with --configs GLOB"
                ]
            examined += 1
            entry = _config_entry(root, relpath)
            if entry is not None:
                entries.append(entry)
    return entries, []


# ------------------------------------------------------------------ status --full


def _experiment_rows(pool: DictRowPool) -> list[dict[str, Any]]:
    """Every Experiment with its run count and latest run.

    The same expressions as the ``base`` CTE of ``triage.experiment_summary`` (migration
    0019), read from the tables. The view also left-joins ``experiment_actuals``, which
    Postgres cannot drop even when none of its columns is selected: on the DirtyDuck
    tutorial that join cost 0.64 s of a status call that has a 0.5 s budget.
    ``experiment show`` reads the full view row for one Experiment.
    """
    with pool.connection() as conn:
        return conn.execute(
            "select e.experiment_hash,"
            "       coalesce(nullif(e.name, ''), triage.auto_experiment_name(e.experiment_hash))"
            "         as name,"
            "       e.problem_type::text as problem_type,"
            "       e.task_framing::text as task_framing,"
            "       count(r.run_id) as n_runs,"
            "       ((array_agg(r.status order by r.started_at desc nulls last))[1])::text"
            "         as last_status,"
            "       max(r.started_at) as last_started_at,"
            "       e.archived_at"
            " from triage.experiments e"
            " left join triage.runs r using (experiment_hash)"
            " group by e.experiment_hash"
            " order by last_started_at desc nulls last, e.experiment_hash"
        ).fetchall()


def _relation_rows(pool: DictRowPool) -> list[dict[str, Any]]:
    from triage.tui.adapters import EST_ROWS_SQL

    with pool.connection() as conn:
        return conn.execute(
            "select c.relname as name,"
            "       case c.relkind when 'r' then 'table' when 'p' then 'partitioned'"
            "                      when 'v' then 'view' else 'matview' end as kind,"
            f"      case when c.relkind = 'v' then null else ({EST_ROWS_SQL})::bigint end"
            "         as est_rows"
            " from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'triage' and c.relkind in ('r', 'p', 'v', 'm')"
            "   and not c.relispartition"
            " order by c.relname"
        ).fetchall()


def full_status(
    db_url: str, root: Path, globs: Sequence[str] = DEFAULT_CONFIG_GLOBS
) -> dict[str, Any]:
    """The ``status --json`` payload plus what an editor needs to draw the whole Project.

    The lynkeus ``Status`` keys are passed through unchanged. ``configs`` needs no database,
    so it is filled even when ``database.connected`` is false; ``experiments``, ``sources``
    and ``relations`` are then empty.

    Args:
        db_url: the project database URL the CLI resolved.
        root: the project root, where config discovery starts.
        globs: config discovery patterns.

    Returns:
        A JSON-ready dict; see ``docs-site/src/content/docs/reference/cli.md``.
    """
    from triage import __version__
    from triage.sources import list_sources
    from triage.tui.adapters import TriageStatus, project_name, source_for

    status = TriageStatus(
        source_for(db_url), project_name(db_url) or "triage-pg"
    ).status()
    configs, warnings = discover_configs(root, globs)
    payload: dict[str, Any] = {
        "contract": CONTRACT,
        "triage_version": __version__,
        "project_root": str(root),
        **status.to_json(),
        "experiments": [],
        "configs": [asdict(c) for c in configs],
        "sources": [],
        "relations": [],
        "warnings": warnings,
    }
    if status.database.connected:
        pool = connection_pool(db_url, min_size=1, max_size=1)
        try:
            payload["experiments"] = _experiment_rows(pool)
            payload["sources"] = list_sources(pool)
            payload["relations"] = _relation_rows(pool)
        finally:
            pool.close()
    return jsonable(payload)


# ------------------------------------------------------------------ experiment show


def match_experiment_hashes(conn: Any, prefix: str, limit: int = 3) -> list[str]:
    """Full experiment hashes that start with ``prefix``, at most ``limit`` of them."""
    rows = conn.execute(
        "select experiment_hash from triage.experiments"
        " where experiment_hash like %(p)s || '%%' order by experiment_hash"
        " limit %(n)s",
        {"p": prefix, "n": limit},
    ).fetchall()
    return [r["experiment_hash"] for r in rows]


def _plan_json(
    config: Mapping[str, Any],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(plan, None)``, or ``(None, error)`` when the config cannot be planned."""
    from triage.adapters.preflight import plan_experiment

    try:
        plan = plan_experiment(config)
    except Exception as exc:  # reported to the caller as plan_error, never swallowed
        logger.debug("plan_experiment failed", exc_info=True)
        return None, _one_line(exc) or type(exc).__name__
    return {
        "n_splits": plan.n_splits,
        "n_test_matrices": plan.n_test_matrices,
        "n_as_of_dates": plan.n_as_of_dates,
        "label_timespans": list(plan.label_timespans),
        "n_matrices": plan.n_matrices,
        "grid_size": plan.grid_size,
        "n_model_groups": plan.n_model_groups,
        "n_runs": plan.n_runs,
        "n_models": plan.n_models,
        "n_feature_columns": plan.n_feature_columns,
        "n_truncated_columns": plan.n_truncated_columns,
        "subsets": [
            {
                "label": s.label,
                "group_names": list(s.group_names),
                "n_columns": s.n_columns,
            }
            for s in plan.subsets
        ],
        "baseline_issues": [issue.describe() for issue in plan.baseline_issues],
        "warnings": list(plan.warnings),
        "feature_error": plan.feature_error,
    }, None


def _history(
    pool: DictRowPool, db_url: str, experiment_hash: str, n_runs: int, n_top: int
) -> Optional[dict[str, Any]]:
    """What the database holds for one Experiment, or ``None`` when it holds nothing."""
    from psycopg import errors as pg_errors

    from triage.diagnostics import readers
    from triage.tui.adapters import TriageRuns, source_for

    with pool.connection() as conn:
        summary = conn.execute(
            "select * from triage.experiment_summary where experiment_hash = %(h)s",
            {"h": experiment_hash},
        ).fetchone()
    if summary is None:
        return None
    runs = TriageRuns(source_for(db_url)).list(n_runs, experiment_hash=experiment_hash)
    chosen = readers.default_metric(pool, experiment_hash)
    groups = (
        [] if chosen is None else readers.model_groups(pool, experiment_hash, *chosen)
    )
    try:
        leaderboard = readers.leaderboard(pool, experiment_hash, limit=n_top)
    except pg_errors.ObjectNotInPrerequisiteState:
        # Never refreshed. This verb only reads; `triage leaderboard --refresh` writes.
        leaderboard = []
    return {
        "summary": summary,
        "runs": [run.to_json() for run in runs],
        "model_group_metric": (
            None if chosen is None else {"metric": chosen[0], "parameter": chosen[1]}
        ),
        "model_groups": groups,
        "leaderboard": leaderboard,
    }


def experiment_object(
    target: str,
    db_url: Optional[str],
    *,
    db_error: Optional[str] = None,
    n_runs: int = 20,
    n_top: int = 10,
) -> dict[str, Any]:
    """One Experiment: its identity, validation, DB-free plan and history in the database.

    Args:
        target: a config file path, or else a prefix of an experiment hash.
        db_url: the project database URL, or ``None`` when the CLI resolved none.
        db_error: why there is no ``db_url``, shown as the database detail.
        n_runs: the newest runs to include.
        n_top: the leaderboard rows to include.

    Returns:
        A JSON-ready dict. ``history`` is ``None`` when the Experiment has no row in the
        database or the database is unreachable (``database.connected`` tells them apart).

    Raises:
        TargetError: ``target`` is not a file and not exactly one known hash prefix.
    """
    from triage.adapters.run import experiment_hash_for, validate_experiment_config
    from triage.tui.adapters import source_for

    health = None if db_url is None else source_for(db_url).health()
    database = {
        "connected": bool(health and health.connected),
        "detail": health.detail if health else (db_error or "no database configured"),
    }
    pool = (
        connection_pool(db_url, min_size=1, max_size=2)
        if db_url is not None and database["connected"]
        else None
    )
    try:
        path = Path(target)
        config_yaml: Optional[str] = None
        if path.is_file():
            source, shown_path = "file", target
            try:
                config = yaml.full_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise TargetError(f"{target}: cannot parse: {_one_line(exc)}") from exc
            if not isinstance(config, Mapping):
                raise TargetError(f"{target} is not a YAML mapping, so not a config")
            experiment_hash = experiment_hash_for(config)
        else:
            if pool is None:
                raise TargetError(
                    f"{target!r} is not a file, and without a reachable database it cannot"
                    f" be looked up as an experiment hash ({database['detail']})"
                )
            with pool.connection() as conn:
                hashes = match_experiment_hashes(conn, target)
                if not hashes:
                    raise TargetError(
                        f"{target!r} is neither a config file nor the prefix of a known"
                        " experiment hash"
                    )
                if len(hashes) > 1:
                    raise TargetError(
                        f"Ambiguous experiment prefix {target!r} — matches"
                        f" {', '.join(h[:16] + '…' for h in hashes)}."
                    )
                row = conn.execute(
                    "select config from triage.experiments where experiment_hash = %(h)s",
                    {"h": hashes[0]},
                ).fetchone()
            source, shown_path, experiment_hash = "database", None, hashes[0]
            config = (row or {}).get("config") or {}
            config_yaml = yaml.safe_dump(dict(config), sort_keys=False)
        validation = validate_experiment_config(config)
        plan, plan_error = _plan_json(config)
        history = (
            None
            if pool is None or db_url is None
            else _history(pool, db_url, experiment_hash, n_runs, n_top)
        )
    finally:
        if pool is not None:
            pool.close()

    # The config's own name, else the one the database shows (auto-named when unset).
    name = config.get("name")
    if name is None and history is not None:
        name = history["summary"].get("name")
    payload: dict[str, Any] = {
        "contract": CONTRACT,
        "source": source,
        "path": shown_path,
        "experiment_hash": experiment_hash,
        "name": None if name is None else str(name),
        "problem_type": config.get("problem_type"),
        "task_framing": config.get("task_framing"),
        "validation": {
            "valid": bool(validation.get("valid")),
            "errors": list(validation.get("errors", [])),
            "warnings": list(validation.get("warnings", [])),
        },
        "plan": plan,
        "plan_error": plan_error,
        "config_yaml": config_yaml,
        "database": database,
        "history": history,
    }
    return jsonable(payload)
