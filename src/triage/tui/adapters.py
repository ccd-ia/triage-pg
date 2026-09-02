"""The triage-pg side of the lynkeus contract: three adapters over the project DB.

Everything the Status and Runs adapters return is a query over what already
exists in the ``triage`` schema — ``triage.runs``, the ``run_progress`` /
``run_summary`` / ``experiment_summary`` views, the artifact tables — never a
stored flag (ADR-0012: no business logic in a UI; ADR-0021: progress is core
telemetry). The Actions adapter lists ``just`` recipes and the typer commands
of ``triage.cli`` and runs them as subprocesses; the shell turns the exit code
into the run's state.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from lynkeus import (
    Action,
    ActionSource,
    Gauge,
    PendingItem,
    PgSource,
    Run,
    RunDetail,
    RunEvent,
    RunState,
    Stage,
    Status,
)
from sqlalchemy.engine import make_url

from triage.logging import get_logger
from triage.util.db import libpq_conninfo

logger = get_logger(__name__)

STALE_RUN_HOURS = 6
POLL_SECONDS = 5.0

#: The nine views the Query screen offers as saved queries (migrations 0004/0005/0013).
SAVED_QUERIES: dict[str, str] = {
    "leaderboard": (
        "select experiment_hash, model_group_id, model_type, metric, parameter,\n"
        "       as_of_date, round(value::numeric, 4) as value\n"
        "from   triage.leaderboard\n"
        "order by as_of_date desc, value desc\nlimit 100;"
    ),
    "run_progress": "select * from triage.run_progress order by run_id, kind, status;",
    "run_summary": (
        "select run_id, status, purpose, profile, started_at, finished_at, duration,\n"
        "       experiment_hash, problem_type\n"
        "from   triage.run_summary\norder by started_at desc\nlimit 50;"
    ),
    "experiment_summary": (
        "select experiment_hash, name, problem_type, task_framing, n_runs,\n"
        "       last_status, n_models, n_model_groups, base_rate, cohort_size\n"
        "from   triage.experiment_summary\norder by last_started_at desc nulls last;"
    ),
    "audition": (
        "select experiment_hash, metric, parameter, model_group_id, n_splits_evaluated,\n"
        "       round(avg_value::numeric, 4) as avg_value,\n"
        "       round(avg_distance_from_best::numeric, 4) as avg_dist_from_best,\n"
        "       round(max_regret::numeric, 4) as max_regret\n"
        "from   triage.audition\norder by experiment_hash, metric, parameter, avg_value desc;"
    ),
    "audition_distances": (
        "select experiment_hash, model_group_id, metric, parameter, as_of_date,\n"
        "       round(raw_value::numeric, 4) as raw_value,\n"
        "       round(dist_from_best_case::numeric, 4) as dist_from_best_case\n"
        "from   triage.audition_distances\norder by as_of_date desc, dist_from_best_case\n"
        "limit 200;"
    ),
    "model_group_summary": (
        "select experiment_hash, model_group_id, model_type, n_models,\n"
        "       first_train_end, last_train_end, hyperparameters\n"
        "from   triage.model_group_summary\norder by model_group_id;"
    ),
    "label_base_rate": (
        "select run_id, as_of_date, label_timespan, round(base_rate::numeric, 4) as base_rate,\n"
        "       n_labeled\nfrom   triage.label_base_rate\norder by run_id, as_of_date;"
    ),
    "cohort_profile": (
        "select run_id, as_of_date, n_entities\nfrom   triage.cohort_profile\n"
        "order by run_id, as_of_date;"
    ),
}

_STATE = {
    "started": RunState.RUNNING,
    "completed": RunState.SUCCEEDED,
    "failed": RunState.FAILED,
}

_RUN_COLUMNS = (
    "r.run_id::text as run_id, r.status::text as status, r.started_at, r.finished_at,"
    " r.purpose, r.error, r.profile, r.git_hash, r.triage_version, r.batch_job_id,"
    " r.plan, r.experiment_hash,"
    " coalesce(nullif(e.name, ''), triage.auto_experiment_name(e.experiment_hash),"
    "          left(r.experiment_hash, 8), 'run') as name"
)


def source_for(db_url: str) -> PgSource:
    """A lynkeus source over the URL ``triage.cli`` resolved (dbfile › yaml › env)."""
    return PgSource(dsn=libpq_conninfo(db_url))


def project_name(db_url: str) -> str:
    """The database segment of the URL — the Project, per ADR-0002."""
    return make_url(db_url).database or ""


def _first_line(text: str | None, width: int = 60) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0]
    return line if len(line) <= width else line[: width - 1] + "…"


def _run_from_row(row: dict[str, Any]) -> Run:
    state = _STATE.get(row["status"], RunState.QUEUED)
    plan = row.get("plan") or {}
    n_models = plan.get("n_models")
    if state is RunState.FAILED:
        detail = _first_line(row.get("error"), 40)
    elif n_models is not None:
        detail = f"{n_models} models"
    else:
        detail = ""
    if row.get("purpose") and row["purpose"] != "experiment":
        detail = f"{row['purpose']} · {detail}".strip(" ·")
    return Run(
        run_id=row["run_id"],
        name=row["name"],
        state=state,
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        detail=detail,
    )


# ------------------------------------------------------------------ status


class TriageStatus:
    """``StatusAdapter``: health, sizes, gauges, last runs, pending work — all queries."""

    def __init__(self, source: PgSource, project: str) -> None:
        self.source = source
        self.project = project

    def status(self) -> Status:
        """One round trip per panel; nothing here is stored anywhere."""
        health = self.source.health()
        if not health.connected:
            return Status(project=self.project, database=health)
        return Status(
            project=self.project,
            database=health,
            last_runs=[_run_from_row(r) for r in self._last_runs()],
            pending=self._pending(),
            extra=self._extra(),
            gauges=self._gauges(),
            series={"runs per day": self._runs_per_day()},
        )

    def _extra(self) -> dict[str, str]:
        row = self.source.rows(
            "select pg_size_pretty(pg_database_size(current_database())) as size,"
            " (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            "   where n.nspname = 'triage' and c.relkind in ('r', 'p')) as n_tables,"
            " (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            "   where n.nspname = 'triage' and c.relkind in ('v', 'm')) as n_views"
        )[0]
        extra = {
            "size": f"{row['size']} · {row['n_tables']} tables · {row['n_views']} views"
        }
        try:
            version = self.source.rows(
                "select version_num from triage.results_schema_versions"
            )
            extra["schema"] = str(version[0]["version_num"]) if version else "unstamped"
        except psycopg.Error as exc:
            logger.debug("results_schema_versions not readable: {}", exc)
            extra["schema"] = "unknown"
        by_status = self.source.rows(
            "select status, count(*) as n from triage.artifacts group by status order by status"
        )
        if by_status:
            extra["artifacts"] = " · ".join(
                f"{r['n']} {r['status']}" for r in by_status
            )
        return extra

    def _gauges(self) -> list[Gauge]:
        exact = self.source.rows(
            "select (select count(*) from triage.experiments) as experiments,"
            " (select count(*) from triage.runs) as runs,"
            " (select count(*) from triage.model_groups) as model_groups,"
            " (select count(*) from triage.models) as models"
        )[0]
        # predictions is partitioned (ADR-0006) and evaluations can be large: planner
        # estimates, summed over partitions, instead of a full count on every poll.
        estimates = {
            r["relname"]: int(r["est"])
            for r in self.source.rows(
                "select c.relname,"
                " case when c.relkind = 'p' then"
                "   (select coalesce(sum(greatest(p.reltuples, 0)), 0) from pg_inherits i"
                "    join pg_class p on p.oid = i.inhrelid where i.inhparent = c.oid)"
                " else greatest(c.reltuples, 0) end as est"
                " from pg_class c join pg_namespace n on n.oid = c.relnamespace"
                " where n.nspname = 'triage'"
                "   and c.relname in ('predictions', 'evaluations')"
            )
        }
        return [
            Gauge("predictions", estimates.get("predictions", 0), note="~"),
            Gauge("evaluations", estimates.get("evaluations", 0), note="~"),
            Gauge("models", exact["models"]),
            Gauge("model groups", exact["model_groups"]),
            Gauge("runs", exact["runs"]),
            Gauge("experiments", exact["experiments"]),
        ]

    def _last_runs(self) -> list[dict[str, Any]]:
        return self.source.rows(
            f"select {_RUN_COLUMNS} from triage.runs r"
            " left join triage.experiments e using (experiment_hash)"
            " order by r.started_at desc limit 5"
        )

    def _runs_per_day(self) -> list[float]:
        rows = self.source.rows(
            "select count(r.run_id) as n"
            " from generate_series(current_date - 13, current_date, interval '1 day') d"
            " left join triage.runs r on r.started_at::date = d::date"
            " group by d order by d"
        )
        return [float(r["n"]) for r in rows]

    def _pending(self) -> list[PendingItem]:
        row = self.source.rows(
            "select"
            " (select count(*) from triage.runs where status = 'started') as running,"
            " (select count(*) from triage.runs where status = 'started'"
            "    and started_at < now() - %(stale)s * interval '1 hour') as stale_runs,"
            " (select count(*) from triage.artifacts a where a.status = 'building'"
            "    and not exists (select 1 from triage.runs r"
            "                    where r.run_id = a.built_by_run and r.status = 'started'))"
            "   as orphan_builds,"
            " (select count(*) from triage.experiments e where e.archived_at is null"
            "    and not exists (select 1 from triage.runs r"
            "                    where r.experiment_hash = e.experiment_hash))"
            "   as idle_experiments,"
            " (select relispopulated from pg_class"
            "    where oid = 'triage.leaderboard'::regclass) as lb_populated,"
            " (select count(*) from triage.evaluations) as n_evaluations",
            {"stale": STALE_RUN_HOURS},
        )[0]
        items: list[PendingItem] = []
        if row["stale_runs"]:
            items.append(
                PendingItem(
                    "runs",
                    f"{row['stale_runs']} still 'started' after {STALE_RUN_HOURS} h",
                    "triage runs list",
                    "error",
                )
            )
        elif row["running"]:
            items.append(PendingItem("runs", f"{row['running']} running", "", "info"))
        if row["orphan_builds"]:
            items.append(
                PendingItem(
                    "artifacts",
                    f"{row['orphan_builds']} stuck in 'building' with no live run",
                    "",
                    "warn",
                )
            )
        if row["lb_populated"] is False and row["n_evaluations"]:
            items.append(
                PendingItem(
                    "leaderboard",
                    "matview not populated",
                    "triage leaderboard <hash> refreshes",
                    "warn",
                )
            )
        elif row["lb_populated"]:
            missing = self.source.rows(
                "select count(*) as n from triage.runs r where r.status = 'completed'"
                " and exists (select 1 from triage.models m"
                "             join triage.evaluations e on e.model_id = m.model_id"
                "             where m.run_id = r.run_id)"
                " and not exists (select 1 from triage.leaderboard l where l.run_id = r.run_id)"
            )[0]["n"]
            if missing:
                items.append(
                    PendingItem(
                        "leaderboard",
                        f"{missing} completed runs not in the matview",
                        "refresh",
                        "warn",
                    )
                )
        if row["idle_experiments"]:
            items.append(
                PendingItem(
                    "experiments",
                    f"{row['idle_experiments']} without a run",
                    "",
                    "info",
                )
            )
        if not items:
            items.append(PendingItem("runs", "nothing pending", "", "ok"))
        return items


# -------------------------------------------------------------------- runs


class TriageRuns:
    """``RunsAdapter`` over ``triage.runs`` + ``run_progress``, LISTEN with a poll fallback."""

    mode = "LISTEN run_progress · poll fallback"

    def __init__(self, source: PgSource, dashboard_url: str | None = None) -> None:
        self.source = source
        self.dashboard_url = (dashboard_url or "").rstrip("/")

    # -- list / show ---------------------------------------------------------------
    def list(self, limit: int = 50) -> list[Run]:
        """Newest first."""
        rows = self.source.rows(
            f"select {_RUN_COLUMNS} from triage.runs r"
            " left join triage.experiments e using (experiment_hash)"
            " order by r.started_at desc limit %(n)s",
            {"n": limit},
        )
        return [_run_from_row(r) for r in rows]

    def resolve(self, prefix: str) -> str:
        """Expand a git-style run-id prefix; raise if it is not unique."""
        rows = self.source.rows(
            "select run_id::text as run_id from triage.runs"
            " where run_id::text like %(p)s order by started_at desc limit 2",
            {"p": prefix + "%"},
        )
        if len(rows) == 1:
            return rows[0]["run_id"]
        if not rows:
            raise LookupError(f"no run matches '{prefix}'")
        raise LookupError(f"'{prefix}' is ambiguous — give more characters")

    def show(self, run_id: str) -> RunDetail:
        """The run row plus a stage table derived from artifacts and the plan."""
        run_id = self.resolve(run_id)
        row = self.source.rows(
            f"select {_RUN_COLUMNS} from triage.runs r"
            " left join triage.experiments e using (experiment_hash)"
            " where r.run_id = %(r)s",
            {"r": run_id},
        )[0]
        return RunDetail(_run_from_row(row), self._stages(run_id, row), self._meta(row))

    def _stages(self, run_id: str, row: dict[str, Any]) -> list[Stage]:
        plan = row.get("plan") or {}
        progress: dict[tuple[str, str], int] = {
            (r["kind"], r["status"]): int(r["n"])
            for r in self.source.rows(
                "select kind::text as kind, status, n from triage.run_progress"
                " where run_id = %(r)s",
                {"r": run_id},
            )
        }
        used: dict[str, int] = {
            r["kind"]: int(r["n"])
            for r in self.source.rows(
                "select a.kind::text as kind, count(*) as n from triage.run_artifacts ra"
                " join triage.artifacts a on a.artifact_id = ra.artifact_id"
                " where ra.run_id = %(r)s group by a.kind",
                {"r": run_id},
            )
        }
        scored = self.source.rows(
            "select"
            " count(*) filter (where exists (select 1 from triage.predictions p"
            "                               where p.model_id = m.model_id)) as predicted,"
            " count(*) filter (where exists (select 1 from triage.evaluations e"
            "                               where e.model_id = m.model_id)) as evaluated"
            " from triage.run_artifacts ra"
            " join triage.models m on m.model_hash = ra.artifact_id"
            " where ra.run_id = %(r)s",
            {"r": run_id},
        )[0]

        def stage(name: str, kind: str, total: int) -> Stage:
            built = progress.get((kind, "built"), 0)
            done = max(built, used.get(kind, 0))
            notes = []
            if progress.get((kind, "building")):
                notes.append(f"{progress[(kind, 'building')]} building")
            if progress.get((kind, "failed")):
                notes.append(f"{progress[(kind, 'failed')]} failed")
            if used.get(kind, 0) > built:
                notes.append(f"{used[kind] - built} cached")
            return Stage(
                name, min(done, total) if total else done, total, " · ".join(notes)
            )

        n_models = int(plan.get("n_models") or 0)
        return [
            stage("cohort", "cohort", 1),
            stage("labels", "labels", 1),
            stage("matrices", "matrix", int(plan.get("n_matrices") or 0)),
            stage("models", "model", n_models),
            Stage("predictions", int(scored["predicted"]), n_models),
            Stage("evaluations", int(scored["evaluated"]), n_models),
        ]

    def _meta(self, row: dict[str, Any]) -> dict[str, str]:
        plan = row.get("plan") or {}
        meta: dict[str, str] = {"profile": row.get("profile") or ""}
        if row.get("git_hash"):
            meta["git"] = str(row["git_hash"])[:7]
        if plan.get("n_splits"):
            groups = plan.get("n_feature_groups") or 1
            meta["splits"] = f"{plan['n_splits']} · {groups} feature group(s)"
        if plan.get("n_features"):
            meta["features"] = str(plan["n_features"])
        if row.get("batch_job_id"):
            meta["batch"] = str(row["batch_job_id"])
        if row.get("experiment_hash"):
            meta["experiment"] = str(row["experiment_hash"])[:8]
        if self.dashboard_url:
            meta["url"] = f"{self.dashboard_url}/runs/{row['run_id']}"
        return meta

    # -- events -------------------------------------------------------------------
    def events(self, run_id: str) -> Iterator[RunEvent | None]:
        """LISTEN ``run_progress`` for a live run; a finished run replays its counts.

        Yields ``None`` every couple of seconds while waiting so the shell can
        stop the stream. If LISTEN fails, falls back to polling the
        ``run_progress`` view every ``POLL_SECONDS`` and emitting the deltas.
        """
        run_id = self.resolve(run_id)
        status = self._status(run_id)
        if status != "started":
            yield from self._replay(run_id, status)
            return
        try:
            for payload in self.source.listen("run_progress", timeout=2.0):
                if payload is None:
                    yield None
                    continue
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                if data.get("run_id") != run_id:
                    continue
                yield RunEvent(
                    datetime.now(),
                    str(data.get("status", "")),
                    str(data.get("kind", "")),
                )
                if data.get("kind") == "run" and data.get("status") in (
                    "completed",
                    "failed",
                ):
                    return
        except psycopg.Error as exc:
            logger.warning("LISTEN run_progress unavailable, polling instead: {}", exc)
            yield RunEvent(
                datetime.now(),
                "fallback",
                "poll",
                f"LISTEN unavailable ({_first_line(str(exc))}) · polling every {POLL_SECONDS:g}s",
            )
            yield from self._poll(run_id)

    def _status(self, run_id: str) -> str:
        rows = self.source.rows(
            "select status::text as status from triage.runs where run_id = %(r)s",
            {"r": run_id},
        )
        return rows[0]["status"] if rows else "missing"

    def _counts(self, run_id: str) -> dict[tuple[str, str], int]:
        return {
            (r["kind"], r["status"]): int(r["n"])
            for r in self.source.rows(
                "select kind::text as kind, status, n from triage.run_progress"
                " where run_id = %(r)s",
                {"r": run_id},
            )
        }

    def _replay(self, run_id: str, status: str) -> Iterator[RunEvent]:
        row = self.source.rows(
            "select started_at, finished_at, error from triage.runs where run_id = %(r)s",
            {"r": run_id},
        )[0]
        at = row["finished_at"] or row["started_at"] or datetime.now()
        for (kind, art_status), n in sorted(self._counts(run_id).items()):
            yield RunEvent(at, art_status, kind, f"×{n}")
        yield RunEvent(at, status, "run", _first_line(row.get("error")))

    def _poll(self, run_id: str) -> Iterator[RunEvent | None]:
        seen = self._counts(run_id)
        while True:
            deadline = time.monotonic() + POLL_SECONDS
            while time.monotonic() < deadline:
                time.sleep(1.0)
                yield None
            now = self._counts(run_id)
            for key, n in sorted(now.items()):
                if n != seen.get(key, 0):
                    yield RunEvent(datetime.now(), key[1], key[0], f"×{n}")
            seen = now
            status = self._status(run_id)
            if status != "started":
                yield RunEvent(datetime.now(), status, "run")
                return

    def cancel(self, run_id: str) -> None:
        """triage-pg has no cancel: a local run is the ``triage run`` process itself."""
        raise NotImplementedError(
            "triage-pg cannot cancel a run from here: stop the `triage run` process "
            "(local) or terminate the Batch job (cloud); `triage runs status` then "
            "backfills the terminal state."
        )


# ------------------------------------------------------------------ actions

_DESTRUCTIVE = {
    "triage gc",
    "triage archive",
    "triage db downgrade",
    "triage project drop",
}
_RECIPE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)(?P<params>[^:#]*):(?P<deps>.*)$")


def parse_just_dump(text: str) -> list[Action]:
    """Turn ``just --dump`` output into actions; the preceding comment is the description."""
    actions: list[Action] = []
    comment = ""
    for line in text.splitlines():
        if line.startswith("#"):
            comment = line.lstrip("#").strip()
            continue
        if not line or line[0].isspace():
            continue
        match = _RECIPE.match(line)
        if match is None:
            comment = ""
            continue
        name = match.group("name")
        if name == "default":
            comment = ""
            continue
        params = match.group("params").split()
        description = comment or f"just {name}"
        if params:
            description += f" · args: {' '.join(params)}"
        actions.append(
            Action(
                f"just {name}",
                description,
                ActionSource.JUST,
                destructive=name.endswith("-clean"),
            )
        )
        comment = ""
    return actions


def cli_actions() -> list[Action]:
    """Every verb of ``triage.cli``, introspected from the typer app."""
    from triage.cli import app as cli_app

    def describe(command: Any) -> str:
        doc = (
            command.help or (command.callback.__doc__ if command.callback else "") or ""
        )
        return _first_line(doc, 80)

    def verb(command: Any) -> str:
        if command.name:
            return command.name
        return command.callback.__name__.replace("_", "-") if command.callback else "?"

    actions: list[Action] = []
    for command in cli_app.registered_commands:
        name = f"triage {verb(command)}"
        actions.append(
            Action(name, describe(command), ActionSource.CLI, name in _DESTRUCTIVE)
        )
    for group in cli_app.registered_groups:
        if group.typer_instance is None:
            continue
        for command in group.typer_instance.registered_commands:
            name = f"triage {group.name} {verb(command)}"
            actions.append(
                Action(name, describe(command), ActionSource.CLI, name in _DESTRUCTIVE)
            )
    return actions


class TriageActions:
    """``ActionsAdapter``: ``just`` recipes + CLI verbs, each run as a subprocess."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = cwd or Path.cwd()

    def list(self) -> list[Action]:
        """Recipes first (when ``just`` and a justfile are present), then the CLI."""
        actions: list[Action] = []
        just = shutil.which("just")
        if just and (self.cwd / "justfile").exists():
            dump = subprocess.run(
                [just, "--dump"],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            if dump.returncode == 0:
                actions.extend(parse_just_dump(dump.stdout))
            else:
                logger.warning("just --dump failed: {}", dump.stderr.strip())
        return actions + cli_actions()

    def run(self, name: str, args: list[str]) -> subprocess.Popen[str]:
        """Start the recipe or verb; stdout+stderr merged, line-buffered, no colour."""
        argv = shlex.split(name) + list(args)
        if argv and argv[0] == "triage" and shutil.which("triage") is None:
            argv = [
                sys.executable,
                "-c",
                "from triage.cli import execute; execute()",
                *argv[1:],
            ]
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "TERM": "dumb"}
        return subprocess.Popen(
            argv,
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )


def stale_after() -> timedelta:
    """How long a 'started' run may sit before Status calls it stale."""
    return timedelta(hours=STALE_RUN_HOURS)
