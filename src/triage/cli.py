from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote as _urlquote

import typer
import yaml
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from triage.adapters.forward import predict_forward
from triage.adapters.retrain import retrain_and_predict
from triage.artifacts import (
    archive_experiment,
    collect,
    delete_outputs,
    gc_candidates,
    purge,
)
from triage.component.catwalk.grid import flatten_grid_config
from triage.component.results_schema import (
    db_history,
    downgrade_db,
    stamp_db,
    upgrade_db,
)
from triage.component.timechop import Timechop
from triage.logging import configure_logging, get_logger
from triage.profiles import load_profile
from triage.sources import (
    bump_source,
    check_drift,
    get_source,
    list_sources,
    register_source,
)
from triage.util.db import connection_pool

logger = get_logger(__name__)
console = Console()

app = typer.Typer(
    help="Manage Triage experiments, results schema, and post-modeling utilities.",
    add_completion=False,
)
db_app = typer.Typer(help="Administer the Triage results schema and helpers.")
app.add_typer(db_app, name="db")
source_app = typer.Typer(
    help="Manage declared data sources and their version pins (ADR-0014)."
)
app.add_typer(source_app, name="source")
project_app = typer.Typer(
    help="Project lifecycle — registry row + database + triage schema (ADR-0002)."
)
app.add_typer(project_app, name="project")
runs_app = typer.Typer(
    help="Inspect runs — AWS Batch status backfill (cloud-profile-spec §7)."
)
app.add_typer(runs_app, name="runs")
postmodel_app = typer.Typer(
    help="Postmodeling diagnostics — compute from the matrix once, persist to PG,"
    " read anywhere (ADR-0011; docs/postmodeling.md)."
)
app.add_typer(postmodel_app, name="postmodel")
model_app = typer.Typer(help="Inspect a single trained model (headless, ADR-0012).")
app.add_typer(model_app, name="model")

DEFAULT_DATABASE_FILE = pathlib.Path("database.yaml")
DEFAULT_SETUP_FILE = pathlib.Path("experiment.py")

# Config-version label shown by `analyze-config`; inlined here now that the inherited
# experiments package (its former home) is removed in the greenfield strip.
CONFIG_VERSION = "v8"


@dataclass
class CLIState:
    """Resolved CLI context. ``db_url`` is ``None`` when no project-DB config was found —
    resolution is lazy so registry-only commands (``triage project …``) run without one;
    ``db_error`` carries the original resolution error for the command that does need it.
    """

    db_url: Optional[str]
    setup_path: Optional[pathlib.Path]
    db_error: Optional[str] = None


def natural_number(value: int) -> int:
    if value <= 0:
        raise typer.BadParameter(f"{value} is not a natural number")
    return value


def parse_date(value: str | datetime) -> datetime:
    # Handle when typer passes the value directly as datetime
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise typer.BadParameter(
            f"{value} is an invalid date (expected YYYY-MM-DD)"
        ) from exc


def load_file_from_store(path: str) -> str:
    """Read a config file's text through the profile storage seam (cloud-profile-spec §3).

    Dispatches by URI scheme, so a local path AND the ``s3://…`` config URI the cloud Batch
    container reads (``triage run --profile cloud --config s3://…``) share one code path.
    Replaces the retired inherited ``Store.factory`` (cloud-profile-spec §3).
    """
    from triage.profiles.storage import storage_for_root

    storage = storage_for_root(path)
    with storage.open_input(path) as fd:
        return fd.read().decode("utf-8")


def load_yaml_from_store(path: str) -> Dict[str, Any]:
    contents = load_file_from_store(path)
    return yaml.full_load(contents) or {}


def resolve_db_url(dbfile: Optional[pathlib.Path]) -> str:
    """Resolve database URL from multiple sources.

    Precedence order:
    1. --dbfile CLI argument
    2. database.yaml in current directory
    3. DATABASE_URL environment variable
    4. PGHOST, PGUSER, PGDATABASE, PGPASSWORD, PGPORT (PostgreSQL standard)
    5. .env file (loaded automatically)
    """
    # Load .env file if it exists in current directory
    env_path = pathlib.Path.cwd() / ".env"
    logger.debug(f"Looking for .env file at: {env_path}")
    logger.debug(f".env file exists: {env_path.exists()}")

    if env_path.exists():
        # override=False: an explicit inline PG*/DATABASE_URL on the command must win over
        # .env (override=True silently clobbered it — DirtyDuck e2e finding 2026-06-21).
        dotenv_loaded = load_dotenv(dotenv_path=env_path, override=False)
        logger.debug(f"dotenv loaded from {env_path}: {dotenv_loaded}")
    else:
        logger.debug("No .env file found, skipping")

    # Try explicit dbfile or default database.yaml
    if dbfile:
        config = yaml.full_load(dbfile.read_text())
    elif DEFAULT_DATABASE_FILE.exists():
        config = yaml.full_load(DEFAULT_DATABASE_FILE.read_text())
    else:
        # Try DATABASE_URL environment variable
        environ_url = os.getenv("DATABASE_URL")
        if environ_url:
            # Convert to psycopg3 driver if using postgresql:// or postgresql+psycopg2://
            if environ_url.startswith("postgresql://"):
                environ_url = environ_url.replace(
                    "postgresql://", "postgresql+psycopg://", 1
                )
            elif environ_url.startswith("postgresql+psycopg2://"):
                environ_url = environ_url.replace(
                    "postgresql+psycopg2://", "postgresql+psycopg://", 1
                )
            return environ_url

        # Try PostgreSQL standard environment variables
        pg_host = os.getenv("PGHOST")
        pg_user = os.getenv("PGUSER")
        pg_database = os.getenv("PGDATABASE")
        pg_password = os.getenv("PGPASSWORD")
        pg_port = os.getenv("PGPORT")

        # Debug: Log what we found
        logger.debug(
            f"Environment variables: PGHOST={pg_host}, PGUSER={pg_user}, PGDATABASE={pg_database}, PGPASSWORD={'***' if pg_password else None}, PGPORT={pg_port}"
        )

        # If we have at least host and database, build URL from environment
        if pg_host and pg_database:
            return _compose_db_url(
                host=pg_host,
                user=pg_user or "postgres",
                password=pg_password,
                database=pg_database,
                port=int(pg_port) if pg_port else 5432,
            )

        # No configuration found
        raise typer.BadParameter(
            "Database connection not provided. Use one of:\n"
            "  --dbfile DATABASE.YAML\n"
            "  DATABASE_URL environment variable\n"
            "  PGHOST, PGUSER, PGDATABASE environment variables (PostgreSQL standard)\n"
            "  database.yaml file in current directory\n"
            "  .env file with PostgreSQL variables"
        )

    # Build URL from yaml config
    try:
        return _compose_db_url(
            host=config["host"],
            user=config["user"],
            password=config["pass"],
            database=config["db"],
            port=config["port"],
        )
    except KeyError as exc:
        raise typer.BadParameter(
            "database.yaml is missing required keys: host, user, pass, port, db"
        ) from exc


def _compose_db_url(
    *,
    host: str,
    user: str,
    database: str,
    port: int,
    password: Optional[str] = None,
) -> str:
    """Build a ``postgresql+psycopg://`` URL (the SQLAlchemy/alembic form; the app pool
    strips ``+psycopg``). Credentials are percent-encoded so special characters survive.
    """
    auth = _urlquote(str(user), safe="")
    if password:
        auth += ":" + _urlquote(str(password), safe="")
    return f"postgresql+psycopg://{auth}@{host}:{port}/{database}"


def resolve_setup_path(setup: Optional[pathlib.Path]) -> Optional[pathlib.Path]:
    if setup:
        return setup
    if DEFAULT_SETUP_FILE.exists():
        return DEFAULT_SETUP_FILE
    return None


def load_setup_module(setup_path: pathlib.Path) -> None:
    logger.info("Loading setup module at %s", setup_path)
    spec = importlib.util.spec_from_file_location("triage_config", str(setup_path))
    if not spec or not spec.loader:
        raise typer.BadParameter(f"Unable to load setup module from {setup_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    logger.info("Setup module loaded")


def get_state(ctx: typer.Context) -> CLIState:
    state = ctx.obj
    if not isinstance(state, CLIState):
        raise RuntimeError("CLI state is not initialized.")
    return state


def require_db_url(ctx: typer.Context) -> str:
    """The resolved project-database URL — raising the deferred resolution error if none."""
    state = get_state(ctx)
    if state.db_url is None:
        raise typer.BadParameter(state.db_error or "Database connection not provided.")
    return state.db_url


def get_pool(ctx: typer.Context):
    return connection_pool(require_db_url(ctx))


def short_description(value: Optional[str]) -> str:
    if not value:
        return "Not provided"
    stripped = " ".join(value.split())
    return textwrap.shorten(stripped, width=120, placeholder="…")


def describe_sql_block(block: Dict[str, Any]) -> str:
    if "query" in block and block["query"]:
        return short_description(block["query"])
    if "filepath" in block and block["filepath"]:
        path = pathlib.Path(block["filepath"])
        if path.exists():
            try:
                return short_description(path.read_text())
            except OSError:
                return f"SQL file at {path} (unreadable)"
        return f"SQL file reference: {block['filepath']}"
    return "Not specified"


def load_experiment_config(config_path: str) -> Dict[str, Any]:
    return load_yaml_from_store(config_path)


@app.callback()
def triage_callback(
    ctx: typer.Context,
    dbfile: Optional[pathlib.Path] = typer.Option(
        None,
        "--dbfile",
        "-d",
        help="YAML file containing database connection information.",
    ),
    setup: Optional[pathlib.Path] = typer.Option(
        None,
        "--setup",
        "-s",
        help="Python module to import before executing commands.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Logging level (TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL).",
        case_sensitive=False,
    ),
) -> None:
    # Reconfigure logging with the requested level
    configure_logging(default_level=log_level.upper())

    # Lazy resolution: registry-only commands (triage project …) must run without a project-DB
    # config, so a resolution failure is stored and raised by the first command that needs it.
    db_error: Optional[str] = None
    try:
        db_url: Optional[str] = resolve_db_url(dbfile)
    except typer.BadParameter as exc:
        db_url, db_error = None, str(exc)
    setup_path = resolve_setup_path(setup)
    if setup_path:
        load_setup_module(setup_path)
    ctx.obj = CLIState(db_url=db_url, setup_path=setup_path, db_error=db_error)
    if db_url is not None:
        logger.info("Using database %s", db_url)
    if setup_path:
        logger.info("Setup module: %s", setup_path)


@app.command("run")
def run_command(
    ctx: typer.Context,
    config: str = typer.Argument(
        ...,
        help="Greenfield experiment config YAML (problem_type, temporal_config,"
        " cohort_config, label_config, feature_config, grid_config, sources)."
        " Under --profile cloud this may be an s3:// URI the container reads.",
    ),
    project_path: pathlib.Path = typer.Option(
        pathlib.Path.cwd(),
        "--project-path",
        help="Local directory to store Parquet matrices + joblib models (local profile)."
        " Cloud derives s3://$TRIAGE_S3_BUCKET from the environment.",
    ),
    random_seed: int = typer.Option(
        0,
        "--random-seed",
        help="Deterministic seed stored on the run + passed to models.",
    ),
    profile: str = typer.Option(
        "local", "--profile", help="Run profile: 'local' or 'cloud' (ADR-0003)."
    ),
    cache_policy: str = typer.Option(
        "exact", "--cache-policy", help="Artifact cache policy: 'exact' or 'logical'."
    ),
) -> None:
    """Run a greenfield experiment end-to-end (cohort→labels→matrix→model→eval, ADR-0012).

    Builds the :class:`~triage.profiles.Profile` from ``--profile`` + the environment, opens the
    pool via ``profile.auth``, and wraps the headless core via ``profile.execution`` — in-process
    locally, or one AWS Batch job submitted (returning the ``job_id`` immediately) in cloud.
    """
    profile_obj = load_profile(
        profile,
        dburl=require_db_url(ctx),
        storage_root=str(project_path) if profile == "local" else None,
    )
    config_data = load_experiment_config(config)

    pool = profile_obj.auth.open_pool()
    try:
        handle = profile_obj.execution.run(
            pool,
            config_data,
            storage=profile_obj.storage,
            storage_root=profile_obj.storage_root,
            random_seed=random_seed,
            profile=profile,
            cache_policy=cache_policy,
        )
    finally:
        pool.close()

    if handle.batch_job_id is not None:
        console.print(
            f"[green]Submitted AWS Batch job[/green] [cyan]{handle.batch_job_id}[/cyan]"
            f" (config staged to {handle.config_uri}). The run completes asynchronously"
            " inside the job; poll Batch for its status."
        )
        return

    result = handle.run_result
    console.print(
        f"[green]Experiment {result.experiment_hash[:12]}… completed:[/green]"
        f" {result.num_runs} run(s), {result.num_models} model(s),"
        f" {result.num_predictions} prediction(s), {result.num_evaluations} evaluation(s)."
    )
    for run in result.runs:
        console.print(
            f"  [cyan]run {str(run.run_id)[:8]}…[/cyan] ([magenta]{run.feature_group}[/magenta]):"
            f" {run.num_models} model(s), {run.num_predictions} prediction(s),"
            f" {run.num_evaluations} evaluation(s)."
        )
    console.print(f"[cyan]storage:[/cyan] {profile_obj.storage_root}")


def _fmt(value, digits: int = 4) -> str:
    """Render a numeric cell — em-dash for NULL (e.g. regret-next-time on a last split)."""
    return "—" if value is None else f"{float(value):.{digits}f}"


def _resolve_experiment_hash(conn, prefix: str) -> str:
    """Resolve a (possibly truncated) experiment hash to the full one — fail loud.

    The run summary prints a 12-char prefix; every inspection command accepts it
    (like git). Ambiguity and unknown prefixes error with next steps.
    """
    rows = conn.execute(
        "select experiment_hash from triage.experiments"
        " where experiment_hash like %(p)s || '%%' order by experiment_hash limit 3",
        {"p": prefix},
    ).fetchall()
    if not rows:
        console.print(
            f"[red]No experiment matches {prefix!r} — list them with:"
            " select experiment_hash, name from triage.experiments;[/red]"
        )
        raise typer.Exit(code=1)
    if len(rows) > 1:
        console.print(
            f"[red]Ambiguous experiment prefix {prefix!r} — matches"
            f" {', '.join(r['experiment_hash'][:16] + '…' for r in rows)}.[/red]"
        )
        raise typer.Exit(code=1)
    return rows[0]["experiment_hash"]


@app.command("leaderboard")
def leaderboard_command(
    ctx: typer.Context,
    experiment_hash: str = typer.Argument(..., help="Experiment (problem) hash."),
    metric: Optional[str] = typer.Option(
        None, "--metric", "-m", help="Filter to one metric (e.g. 'precision@')."
    ),
    parameter: Optional[str] = typer.Option(
        None, "--parameter", "-p", help="Filter to one parameter (e.g. '100_abs')."
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows to print."),
    as_json: bool = typer.Option(False, "--json", help="Print raw rows as JSON."),
) -> None:
    """The experiment leaderboard, headless (ADR-0012) — the same ``triage.leaderboard``
    materialized view the dashboard reads (migration 0005).

    The matview is created WITH NO DATA and refreshed at run end; if no run has populated
    it yet this command refreshes it once and retries.
    """
    from psycopg import errors as pg_errors

    engine = get_pool(ctx)
    sql = (
        "select model_group_id, model_type, split_kind, metric, parameter, as_of_date,"
        "       value, value_expected, value_std, model_id, train_end_time"
        " from triage.leaderboard where experiment_hash = %(hash)s"
    )
    params: Dict[str, Any] = {"hash": experiment_hash, "limit": limit}
    if metric:
        sql += " and metric = %(metric)s"
        params["metric"] = metric
    if parameter is not None:
        sql += " and parameter = %(parameter)s"
        params["parameter"] = parameter
    sql += " order by metric, parameter, as_of_date desc, value desc nulls last"
    sql += " limit %(limit)s"
    with engine.connection() as conn:
        params["hash"] = _resolve_experiment_hash(conn, experiment_hash)
        try:
            rows = conn.execute(sql, params).fetchall()
        except pg_errors.ObjectNotInPrerequisiteState:
            conn.rollback()
            conn.execute("refresh materialized view triage.leaderboard")
            rows = conn.execute(sql, params).fetchall()
        else:
            if not rows:
                # Populated but possibly STALE (e.g. a migration refreshed it before this
                # experiment evaluated) — refresh once and retry before reporting empty.
                conn.execute("refresh materialized view triage.leaderboard")
                rows = conn.execute(sql, params).fetchall()
    if as_json:
        console.print_json(json.dumps(rows, default=str))
        return
    if not rows:
        console.print(
            "[yellow]No leaderboard rows — unknown experiment hash, or nothing has"
            " evaluated yet.[/yellow]"
        )
        return
    table = Table(
        title=f"Leaderboard — experiment {experiment_hash[:12]}…", box=box.SIMPLE_HEAVY
    )
    for col in ("Group", "Model", "Algorithm", "Metric", "Param", "As-of", "Value"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["model_group_id"]),
            str(r["model_id"]),
            str(r["model_type"]).rsplit(".", 1)[-1],
            r["metric"],
            r["parameter"],
            str(r["as_of_date"]),
            _fmt(r["value"]),
        )
    console.print(table)


@app.command("models")
def models_command(
    ctx: typer.Context,
    experiment_hash: str = typer.Argument(..., help="Experiment (problem) hash."),
    metric: Optional[str] = typer.Option(
        None,
        "--metric",
        "-m",
        help="Metric (default: the experiment's most-evaluated).",
    ),
    parameter: Optional[str] = typer.Option(
        None, "--parameter", "-p", help="Metric parameter (e.g. '100_abs')."
    ),
    group: Optional[int] = typer.Option(
        None, "--group", "-g", help="Drill into one model group's members."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print raw rows as JSON."),
) -> None:
    """Model groups at a glance (avg ± σ, max regret, avg fit time) — or, with
    ``--group``, the group's member models with each one's Δ vs the group mean.

    Headless twin of the dashboard's Model Groups tab / group sheet (ADR-0012): the
    numbers come from the same ``triage.audition`` / ``evaluations_windowed`` objects.
    """
    engine = get_pool(ctx)
    with engine.connection() as conn:
        experiment_hash = _resolve_experiment_hash(conn, experiment_hash)
        if metric is None or parameter is None:
            sql = (
                "select metric, parameter, sum(n_splits_evaluated) as n"
                " from triage.audition where experiment_hash = %(h)s"
            )
            if metric is not None:
                sql += " and metric = %(m)s"
            sql += (
                " group by metric, parameter order by n desc, metric, parameter limit 1"
            )
            row = conn.execute(sql, {"h": experiment_hash, "m": metric}).fetchone()
            if row is None:
                console.print(
                    "[yellow]No evaluated test splits for this experiment yet.[/yellow]"
                )
                raise typer.Exit(code=1)
            metric = metric or row["metric"]
            parameter = parameter if parameter is not None else row["parameter"]
        params = {"h": experiment_hash, "m": metric, "p": parameter, "g": group}

        if group is None:
            rows = conn.execute(
                "select mgs.model_group_id, mgs.model_type, mgs.n_models,"
                "       a.avg_value, a.stddev_value, a.max_regret,"
                "       (select avg(mm.train_duration_ms)::bigint from triage.models mm"
                "          join triage.runs rr on rr.run_id = mm.run_id"
                "         where mm.model_group_id = mgs.model_group_id"
                "           and rr.experiment_hash = %(h)s) as avg_duration_ms"
                " from triage.model_group_summary mgs"
                " left join triage.audition a"
                "        on a.experiment_hash = mgs.experiment_hash"
                "       and a.model_group_id = mgs.model_group_id"
                "       and a.metric = %(m)s and a.parameter = %(p)s"
                " where mgs.experiment_hash = %(h)s"
                " order by a.avg_distance_from_best asc nulls last, mgs.model_group_id",
                params,
            ).fetchall()
            if as_json:
                console.print_json(json.dumps(rows, default=str))
                return
            table = Table(
                title=(
                    f"Model groups — experiment {experiment_hash[:12]}…"
                    f"  ({metric}{parameter and ' ' + parameter or ''})"
                ),
                box=box.SIMPLE_HEAVY,
            )
            for col in (
                "Group",
                "Algorithm",
                "Models",
                "Avg ± σ",
                "Max regret",
                "Avg fit",
            ):
                table.add_column(col)
            for r in rows:
                dur = r["avg_duration_ms"]
                table.add_row(
                    str(r["model_group_id"]),
                    str(r["model_type"]).rsplit(".", 1)[-1],
                    str(r["n_models"]),
                    f"{_fmt(r['avg_value'])} ± {_fmt(r['stddev_value'])}",
                    _fmt(r["max_regret"]),
                    f"{dur / 1000:.1f}s" if dur is not None else "—",
                )
            console.print(table)
            return

        group_avg = (
            conn.execute(
                "select avg_value from triage.audition"
                " where experiment_hash = %(h)s and model_group_id = %(g)s"
                "   and metric = %(m)s and parameter = %(p)s",
                params,
            ).fetchone()
            or {}
        ).get("avg_value")
        rows = conn.execute(
            "select m.model_id, m.train_end_time, m.train_duration_ms, w.value_mean"
            " from triage.models m"
            " join triage.runs r on r.run_id = m.run_id"
            " left join triage.evaluations_windowed w"
            "        on w.model_id = m.model_id and w.split_kind = 'test'"
            "       and w.subset_hash = '' and w.metric = %(m)s and w.parameter = %(p)s"
            " where m.model_group_id = %(g)s and r.experiment_hash = %(h)s"
            " order by m.train_end_time, m.model_id",
            params,
        ).fetchall()
        if as_json:
            console.print_json(
                json.dumps({"group_avg": group_avg, "models": rows}, default=str)
            )
            return
        table = Table(
            title=(
                f"Group {group} members — {metric}{parameter and ' ' + parameter or ''}"
                + (f" (group avg {group_avg:.4f})" if group_avg is not None else "")
            ),
            box=box.SIMPLE_HEAVY,
        )
        for col in ("Model", "Train ≤", "Value (window mean)", "Δ vs group", "Fit"):
            table.add_column(col)
        for r in rows:
            delta = (
                r["value_mean"] - group_avg
                if r["value_mean"] is not None and group_avg is not None
                else None
            )
            dur = r["train_duration_ms"]
            table.add_row(
                str(r["model_id"]),
                str(r["train_end_time"] or "—"),
                _fmt(r["value_mean"]),
                f"{delta:+.4f}" if delta is not None else "—",
                f"{dur / 1000:.1f}s" if dur is not None else "—",
            )
        console.print(table)


@model_app.command("show")
def model_show(
    ctx: typer.Context,
    model_id: int = typer.Argument(..., callback=natural_number),
    as_json: bool = typer.Option(False, "--json", help="Print the raw card as JSON."),
) -> None:
    """One model's card, headless: identity, windowed evaluations, top importances,
    calibration deciles, and the top crosstab features when a diagnostics pass ran."""
    engine = get_pool(ctx)
    with engine.connection() as conn:
        card = conn.execute(
            "select m.model_id, m.model_group_id, mg.model_type, mg.hyperparameters,"
            "       m.train_end_time, m.training_label_timespan::text as label_timespan,"
            "       m.train_duration_ms, m.model_size_bytes"
            " from triage.models m"
            " join triage.model_groups mg on mg.model_group_id = m.model_group_id"
            " where m.model_id = %(m)s",
            {"m": model_id},
        ).fetchone()
        if card is None:
            console.print(f"[red]No model with id {model_id}.[/red]")
            raise typer.Exit(code=1)
        windowed = conn.execute(
            "select metric, parameter, n_as_of_dates, value_mean, value_min, value_max"
            " from triage.evaluations_windowed"
            " where model_id = %(m)s and split_kind = 'test' and subset_hash = ''"
            " order by metric, parameter",
            {"m": model_id},
        ).fetchall()
        importances = conn.execute(
            "select feature, feature_importance from triage.feature_importances"
            " where model_id = %(m)s order by rank_abs nulls last limit 10",
            {"m": model_id},
        ).fetchall()
        calibration = []
        cal_date = conn.execute(
            "select max(as_of_date) as d from triage.evaluations"
            " where model_id = %(m)s and split_kind = 'test' and subset_hash = ''",
            {"m": model_id},
        ).fetchone()["d"]
        if cal_date is not None and card["label_timespan"] is not None:
            calibration = conn.execute(
                "select decile, n, avg_score, realized_rate"
                " from triage.monitoring_calibration(%(m)s, 'test', %(d)s,"
                "        cast(%(t)s as interval)) order by decile",
                {"m": model_id, "d": str(cal_date), "t": card["label_timespan"]},
            ).fetchall()
        crosstabs = conn.execute(
            "select feature, ratio from triage.crosstabs"
            " where model_id = %(m)s and stat = 'mean'"
            "   and ratio is not null and ratio > 0"
            " order by abs(ln(ratio)) desc limit 5",
            {"m": model_id},
        ).fetchall()

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "card": card,
                    "windowed": windowed,
                    "importances": importances,
                    "calibration": calibration,
                    "crosstabs": crosstabs,
                },
                default=str,
            )
        )
        return

    hp = json.dumps(card["hyperparameters"] or {}, sort_keys=True)
    console.print(
        Panel.fit(
            f"[bold]{str(card['model_type']).rsplit('.', 1)[-1]}[/bold]"
            f" · group {card['model_group_id']}\n"
            f"hyperparameters: {hp[:100]}\n"
            f"trained ≤ {card['train_end_time']}"
            f" · label window {card['label_timespan'] or '—'}"
            + (
                f" · fit {card['train_duration_ms'] / 1000:.1f}s"
                if card["train_duration_ms"] is not None
                else ""
            ),
            title=f"model {model_id}",
        )
    )
    if windowed:
        t = Table(title="Evaluations (test window)", box=box.SIMPLE_HEAVY)
        for col in ("Metric", "Param", "Dates", "Mean", "Min", "Max"):
            t.add_column(col)
        for w in windowed:
            t.add_row(
                w["metric"],
                w["parameter"],
                str(w["n_as_of_dates"]),
                _fmt(w["value_mean"]),
                _fmt(w["value_min"]),
                _fmt(w["value_max"]),
            )
        console.print(t)
    if importances:
        t = Table(title="Top features", box=box.SIMPLE_HEAVY)
        t.add_column("Feature")
        t.add_column("Importance")
        for f in importances:
            t.add_row(f["feature"], _fmt(f["feature_importance"]))
        console.print(t)
    if calibration:
        t = Table(title=f"Calibration @ {cal_date}", box=box.SIMPLE_HEAVY)
        for col in ("Decile", "n", "Mean score", "Realized"):
            t.add_column(col)
        for c in calibration:
            t.add_row(
                str(c["decile"]),
                str(c["n"]),
                _fmt(c["avg_score"]),
                _fmt(c["realized_rate"]),
            )
        console.print(t)
    if crosstabs:
        t = Table(title="Crosstabs (top |log ratio|)", box=box.SIMPLE_HEAVY)
        t.add_column("Feature")
        t.add_column("Ratio")
        for c in crosstabs:
            t.add_row(c["feature"], _fmt(c["ratio"], 2))
        console.print(t)
    else:
        console.print(
            "[dim]no crosstabs persisted — run `triage postmodel crosstabs"
            f" {model_id}`[/dim]"
        )


@app.command("audition")
def audition_command(
    ctx: typer.Context,
    experiment_hash: str = typer.Argument(
        ..., help="Experiment (problem) hash to audition."
    ),
    metric: Optional[str] = typer.Option(
        None,
        "--metric",
        "-m",
        help="Metric (default: the experiment's most-evaluated).",
    ),
    parameter: Optional[str] = typer.Option(
        None, "--parameter", "-p", help="Metric parameter (e.g. '100_abs')."
    ),
    rule: str = typer.Option(
        "best_average_value",
        "--rule",
        help="Selection rule for the headline pick (any of the 8 standard rules).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the raw result as JSON."),
) -> None:
    """Model selection over the in-PG audition views (ADR-0007), headless (ADR-0012).

    Replaces the retired config-file audition (``component/audition``): the ranking
    (avg±σ, distance-from-best, max regret, regret-next-time), one pick per standard
    selection rule, and the audition-vs-leaderboard divergence — straight from
    ``triage.audition`` / ``audition_pick()`` / ``selected_model()``, the same objects
    the dashboard's Audition tab renders.
    """
    from triage.component.catwalk.in_pg_evaluation import AUDITION_RULES

    rule_names = [name for name, _ in AUDITION_RULES]
    if rule not in rule_names:
        raise typer.BadParameter(
            f"unknown audition rule {rule!r} — one of: {', '.join(rule_names)}"
        )

    engine = get_pool(ctx)
    with engine.connection() as conn:
        experiment_hash = _resolve_experiment_hash(conn, experiment_hash)
        # Default (metric, parameter): the experiment's most-evaluated pair.
        if metric is None or parameter is None:
            sql = (
                "select metric, parameter, sum(n_splits_evaluated) as n"
                " from triage.audition where experiment_hash = %(h)s"
            )
            if metric is not None:
                sql += " and metric = %(m)s"
            sql += (
                " group by metric, parameter order by n desc, metric, parameter limit 1"
            )
            row = conn.execute(sql, {"h": experiment_hash, "m": metric}).fetchone()
            if row is None:
                console.print(
                    "[yellow]No evaluated test splits for this experiment yet —"
                    " audition needs evaluations.[/yellow]"
                )
                raise typer.Exit(code=1)
            metric = metric or row["metric"]
            parameter = parameter if parameter is not None else row["parameter"]

        params = {"h": experiment_hash, "m": metric, "p": parameter}
        ranking = conn.execute(
            "select model_group_id, n_splits_evaluated, avg_value, stddev_value,"
            "       avg_distance_from_best, max_regret,"
            "       avg_regret_next_time, max_regret_next_time"
            " from triage.audition"
            " where experiment_hash = %(h)s and metric = %(m)s and parameter = %(p)s"
            " order by avg_distance_from_best asc, max_regret asc, model_group_id asc",
            params,
        ).fetchall()
        if not ranking:
            console.print(
                f"[yellow]No audition rows for metric {metric!r}"
                f" parameter {parameter!r}.[/yellow]"
            )
            raise typer.Exit(code=1)

        # One pick per standard rule (mirrors the dashboard's strategy panel).
        # best_average_two_metrics needs a SECOND metric — any other pair present.
        second = conn.execute(
            "select metric, parameter from triage.metric_catalog"
            " where not (metric = %(m)s and parameter = %(p)s)"
            " order by metric, parameter limit 1",
            params,
        ).fetchone()
        strategies: List[Dict[str, Any]] = []
        for rule_name, rule_params in AUDITION_RULES:
            if rule_name == "best_average_two_metrics":
                if second is None:
                    continue
                rule_params = {
                    "metric2": second["metric"],
                    "parameter2": second["parameter"],
                    "metric1_weight": 0.5,
                }
            gid = conn.execute(
                "select triage.audition_pick(%(h)s, %(m)s, %(p)s, %(r)s,"
                " %(rp)s::jsonb) as model_group_id",
                {**params, "r": rule_name, "rp": json.dumps(rule_params)},
            ).fetchone()
            strategies.append(
                {"rule": rule_name, "model_group_id": gid["model_group_id"]}
            )

        selected = conn.execute(
            "select * from triage.selected_model(%(h)s, %(m)s, %(p)s, %(r)s)",
            {**params, "r": rule},
        ).fetchone()

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "experiment_hash": experiment_hash,
                    "metric": metric,
                    "parameter": parameter,
                    "rule": rule,
                    "ranking": ranking,
                    "strategies": strategies,
                    "selected": selected,
                },
                default=str,
            )
        )
        return

    pick = next((s["model_group_id"] for s in strategies if s["rule"] == rule), None)
    table = Table(
        title=(
            f"Audition — experiment {experiment_hash[:12]}…"
            f"  ({metric}{parameter and ' ' + parameter or ''})"
        ),
        box=box.SIMPLE_HEAVY,
    )
    for col in (
        "Group",
        "Splits",
        "Avg ± σ",
        "Dist. from best (avg)",
        "Max regret",
        "Regret next time (max)",
    ):
        table.add_column(col)
    for r in ranking:
        style = "bold green" if r["model_group_id"] == pick else ""
        table.add_row(
            str(r["model_group_id"]),
            str(r["n_splits_evaluated"]),
            f"{_fmt(r['avg_value'])} ± {_fmt(r['stddev_value'])}",
            _fmt(r["avg_distance_from_best"]),
            _fmt(r["max_regret"]),
            _fmt(r["max_regret_next_time"]),
            style=style,
        )
    console.print(table)

    st = Table(title="Selection rules — pick per rule", box=box.SIMPLE_HEAVY)
    st.add_column("Rule")
    st.add_column("Picked group")
    for s in strategies:
        style = "bold green" if s["rule"] == rule else ""
        st.add_row(s["rule"], str(s["model_group_id"]), style=style)
    console.print(st)

    if selected is not None:
        if selected["diverges"]:
            console.print(
                f"[yellow]⚠ audition pick (group {selected['audition_group']},"
                f" model {selected['audition_model']}) DIVERGES from leaderboard #1"
                f" (group {selected['leaderboard_group']},"
                f" model {selected['leaderboard_model']}).[/yellow]"
            )
        else:
            console.print(
                f"[green]audition pick and leaderboard #1 agree: group"
                f" {selected['audition_group']}, model"
                f" {selected['audition_model']}.[/green]"
            )


@app.command("retrainpredict")
def retrain_predict_command(
    ctx: typer.Context,
    model_group_id: int = typer.Argument(..., callback=natural_number),
    prediction_date: datetime = typer.Argument(..., callback=parse_date),
    project_path: pathlib.Path = typer.Option(
        pathlib.Path.cwd(), "--project-path", help="Artifact storage path."
    ),
) -> None:
    engine = get_pool(ctx)
    retrain_and_predict(
        engine,
        model_group_id,
        prediction_date.date(),
        storage_dir=str(project_path),
    )
    console.print("[green]Retrain and predict completed.[/green]")


@app.command("predictlist")
def predictlist_command(
    ctx: typer.Context,
    model_id: int = typer.Argument(..., callback=natural_number),
    as_of_date: datetime = typer.Argument(..., callback=parse_date),
    project_path: pathlib.Path = typer.Option(
        pathlib.Path.cwd(), "--project-path", help="Artifact storage path."
    ),
) -> None:
    engine = get_pool(ctx)
    predict_forward(
        engine,
        model_id,
        as_of_date.date(),
        storage_dir=str(project_path),
    )
    console.print("[green]Prediction list generated.[/green]")


@app.command("score")
def score_command(
    ctx: typer.Context,
    model_id: int = typer.Argument(..., callback=natural_number),
    prediction_date: Optional[datetime] = typer.Argument(
        None,
        help="Prediction date (YYYY-MM-DD). Omitted = today — so a cron/EventBridge line"
        " needs no date arithmetic.",
    ),
    project_path: pathlib.Path = typer.Option(
        pathlib.Path.cwd(), "--project-path", help="Artifact storage path."
    ),
) -> None:
    """Forward-score a model (the ADR-0027 monitoring entrypoint; alias of predictlist).

    Safe to re-invoke: predictions are append-only (ADR-0006) and the run records
    purpose='forward_score' + the prediction date (ADR-0018). Schedule it with the
    operator's scheduler — cron locally, EventBridge→Batch in cloud (docs/monitoring.md).
    """
    when = (
        parse_date(prediction_date).date()
        if prediction_date is not None
        else datetime.now().date()
    )
    engine = get_pool(ctx)
    predict_forward(engine, model_id, when, storage_dir=str(project_path))
    console.print(
        f"[green]Forward-scored model {model_id} at {when} (append-only).[/green]"
    )


@app.command("analyze-config")
def analyze_config(
    config: str = typer.Argument(..., help="Experiment config to inspect."),
) -> None:
    config_data = load_experiment_config(config)
    temporal = config_data.get("temporal_config")
    if not temporal:
        console.print("[red]temporal_config block is required.[/red]")
        raise typer.Exit(code=1)

    chopper = Timechop(**temporal)
    matrix_sets = chopper.chop_time()
    total_train = len(matrix_sets)
    total_test = sum(len(m["test_matrices"]) for m in matrix_sets)
    as_of_counts = [
        len(matrix["train_matrix"]["as_of_times"]) for matrix in matrix_sets
    ]
    avg_train_as_of = sum(as_of_counts) / total_train if total_train else 0

    label_config = config_data.get("label_config", {})
    cohort_config = config_data.get("cohort_config", {})

    table = Table(title="Experiment Overview", box=box.SIMPLE_HEAVY)
    table.add_column("Statistic")
    table.add_column("Value", justify="right")
    table.add_row("Config Version", config_data.get("config_version", CONFIG_VERSION))
    table.add_row(
        "Feature Aggregations", str(len(config_data.get("feature_aggregations", [])))
    )
    table.add_row("Cohorts", "1" if cohort_config else "Default (labels-driven)")
    table.add_row("Train matrix sets", str(total_train))
    table.add_row("Test matrices", str(total_test))
    table.add_row("Avg train as_of dates", f"{avg_train_as_of:.1f}")

    grid_config = config_data.get("grid_config")
    if grid_config:
        grid_size = sum(1 for _ in flatten_grid_config(grid_config))
        table.add_row("Model grid size", str(grid_size))

    console.print(table)

    label_panel = Panel.fit(
        f"[cyan]Label name:[/cyan] {label_config.get('name', 'default')}\n"
        f"[cyan]Description:[/cyan] {short_description(label_config.get('description'))}\n"
        f"[cyan]SQL:[/cyan] {describe_sql_block(label_config)}",
        title="Label Configuration",
    )
    console.print(label_panel)

    cohort_panel = Panel.fit(
        f"[cyan]Cohort name:[/cyan] {cohort_config.get('name', 'all_entities')}\n"
        f"[cyan]SQL:[/cyan] {describe_sql_block(cohort_config)}",
        title="Cohort Configuration",
    )
    console.print(cohort_panel)


@db_app.command("upgrade")
def db_upgrade(
    ctx: typer.Context,
    revision: str = typer.Option(
        "head",
        "--revision",
        "-r",
        help="Target schema revision (default head).",
    ),
) -> None:
    upgrade_db(revision=revision, dburl=require_db_url(ctx))
    console.print("[green]Database upgraded.[/green]")


@db_app.command("downgrade")
def db_downgrade(
    ctx: typer.Context,
    revision: str = typer.Option(
        "-1", "--revision", "-r", help="Schema revision to downgrade to."
    ),
) -> None:
    downgrade_db(revision=revision, dburl=require_db_url(ctx))
    console.print("[green]Database downgraded.[/green]")


@db_app.command("stamp")
def db_stamp(
    ctx: typer.Context,
    revision: str = typer.Argument(..., help="Revision to stamp the DB with."),
) -> None:
    stamp_db(revision=revision, dburl=require_db_url(ctx))
    console.print("[green]Database stamped.[/green]")


@db_app.command("history")
def db_history_command(ctx: typer.Context) -> None:
    db_history(dburl=require_db_url(ctx))


@db_app.command("up")
def db_up_command(
    password: bool = typer.Option(
        False,
        "--password",
        help="Prompt for a password when provisioning the container.",
    ),
) -> None:
    inspect = subprocess.run(
        [
            "docker",
            "container",
            "inspect",
            "-f",
            "{{.State.Status}}",
            "triage_db",
        ],
        capture_output=True,
        text=True,
    )

    if inspect.returncode != 0:
        console.print("[yellow]Provisioning new Postgres container...[/yellow]")
        if DEFAULT_DATABASE_FILE.exists():
            console.print(
                "[red]database.yaml already exists; refusing to overwrite.[/red]"
            )
            raise typer.Exit(1)
        db_password = ""
        if password:
            db_password = typer.prompt(
                "Enter a password for your new database user", hide_input=True
            )
        run = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "-p",
                "5432:5432",
                "-e",
                "POSTGRES_HOST=0.0.0.0",
                "-e",
                "POSTGRES_USER=triage_user",
                "-e",
                "POSTGRES_PORT=5432",
                "-e",
                f"POSTGRES_PASSWORD={db_password}",
                "-e",
                "POSTGRES_DB=triage",
                "-v",
                "triage-db-data:/var/lib/postgresql/data",
                "--name",
                "triage_db",
                "postgres:12",
            ],
            capture_output=True,
            text=True,
        )
        if run.returncode != 0:
            console.print(f"[red]Docker run failed: {run.stderr}[/red]")
            raise typer.Exit(1)
        config = {
            "host": "0.0.0.0",
            "user": "triage_user",
            "pass": db_password,
            "port": 5432,
            "db": "triage",
        }
        DEFAULT_DATABASE_FILE.write_text(yaml.dump(config))
        console.print(
            "[green]Database created. Credentials written to database.yaml.[/green]"
        )
    elif "running" in inspect.stdout:
        console.print("[green]triage_db container is already running.[/green]")
    else:
        console.print("[yellow]Starting existing triage_db container.[/yellow]")
        subprocess.run(["docker", "start", "triage_db"], check=True)


@source_app.command("register")
def source_register(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Source name used in configs."),
    relation: str = typer.Option(
        ...,
        "--relation",
        "-r",
        help="Schema-qualified relation the source points at, e.g. semantic.events.",
    ),
    knowledge_date_column: Optional[str] = typer.Option(
        None,
        "--knowledge-date-column",
        "-k",
        help="Column used for the advisory max() fingerprint.",
    ),
    description: Optional[str] = typer.Option(None, "--description"),
    role: Optional[str] = typer.Option(
        None,
        "--role",
        help="Source role: 'entity' (one-row-per-entity attributes) or 'event'.",
    ),
) -> None:
    """Declare a source table (idempotent)."""
    engine = get_pool(ctx)
    register_source(
        engine,
        name,
        relation,
        knowledge_date_column=knowledge_date_column,
        description=description,
        role=role,
    )
    console.print(f"[green]Source '{name}' registered -> {relation}[/green]")


@source_app.command("bump")
def source_bump(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Registered source to pin."),
    version_label: Optional[str] = typer.Option(
        None,
        "--version",
        "-v",
        help="Version label for this load (default: UTC timestamp).",
    ),
) -> None:
    """Record a new version pin after a data load."""
    engine = get_pool(ctx)
    label = bump_source(engine, name, version_label)
    console.print(f"[green]Source '{name}' pinned at '{label}'.[/green]")


@source_app.command("list")
def source_list(ctx: typer.Context) -> None:
    """List sources with their current pins (unpinned sources are volatile)."""
    engine = get_pool(ctx)
    sources = list_sources(engine)
    if not sources:
        console.print("[yellow]No sources registered.[/yellow]")
        return
    table = Table(title="Declared Sources", box=box.SIMPLE_HEAVY)
    table.add_column("Source")
    table.add_column("Relation")
    table.add_column("Current pin")
    table.add_column("Pinned at")
    for source in sources:
        pin = source["version_label"] or "[red]UNPINNED (volatile)[/red]"
        pinned_at = str(source["registered_at"]) if source["registered_at"] else ""
        table.add_row(source["source_name"], source["relation"], pin, pinned_at)
    console.print(table)


@source_app.command("show")
def source_show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Source to inspect."),
) -> None:
    """Show a source's registration, current pin, and drift status."""
    engine = get_pool(ctx)
    source = get_source(engine, name)
    if source is None:
        console.print(f"[red]Source '{name}' is not registered.[/red]")
        raise typer.Exit(code=1)
    drifted = check_drift(engine, name)
    body = "\n".join(f"[cyan]{key}:[/cyan] {value}" for key, value in source.items())
    if drifted:
        body += "\n[red]DRIFT: data changed since the current pin.[/red]"
    console.print(Panel.fit(body, title=f"Source: {name}"))


def _registry_pool_from_env():
    """Open a pool on the registry control plane (``TRIAGE_REGISTRY_URL``) — fail fast."""
    from triage import project_lifecycle

    try:
        url = project_lifecycle.registry_url_from_env()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return url, connection_pool(url)


@project_app.command("create")
def project_create(
    slug: str = typer.Argument(
        ..., help="url-safe id; also names the per-project database (ADR-0002)."
    ),
    display_name: Optional[str] = typer.Option(
        None, "--display-name", help="Human-facing name (defaults to the slug)."
    ),
    database_name: Optional[str] = typer.Option(
        None, "--database-name", help="Target database name (defaults to the slug)."
    ),
) -> None:
    """Create a project end-to-end: registry row → CREATE DATABASE → triage schema (head).

    Needs TRIAGE_REGISTRY_URL (the control plane) and a maintenance connection —
    TRIAGE_MAINT_URL, else the registry cluster's 'postgres' database (ADR-0002).
    """
    from triage import project_lifecycle

    registry_url, pool = _registry_pool_from_env()
    try:
        maint_url = project_lifecycle.maintenance_url(registry_url)
        project = project_lifecycle.create_project(
            pool,
            slug=slug,
            maint_url=maint_url,
            display_name=display_name,
            database_name=database_name,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        pool.close()
    console.print(
        f"[green]Project '{project['slug']}' created:[/green] database"
        f" [cyan]{project['database_name']}[/cyan] provisioned + triage schema at head."
        " Run experiments against it with PG*/DATABASE_URL pointed at that database."
    )


@project_app.command("drop")
def project_drop(
    slug: str = typer.Argument(..., help="Project to tear down."),
    confirm: str = typer.Option(
        ...,
        "--confirm",
        help="Repeat the slug exactly to confirm the irreversible DROP DATABASE.",
    ),
) -> None:
    """DROP the project's database (WITH FORCE) and tombstone its registry row (ADR-0002)."""
    from triage import project_lifecycle

    registry_url, pool = _registry_pool_from_env()
    try:
        maint_url = project_lifecycle.maintenance_url(registry_url)
        project = project_lifecycle.drop_project(
            pool, slug=slug, confirm=confirm, maint_url=maint_url
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        pool.close()
    console.print(
        f"[green]Project '{project['slug']}' dropped[/green] — database"
        f" [cyan]{project['database_name']}[/cyan] removed; registry row kept as a"
        f" tombstone (dropped_at {project['dropped_at']})."
    )


@project_app.command("list")
def project_list(
    show_all: bool = typer.Option(
        False, "--all", help="Include archived and dropped projects."
    ),
) -> None:
    """List registry projects (active only by default)."""
    from triage import registry as registry_module

    _, pool = _registry_pool_from_env()
    try:
        projects = registry_module.list_projects(pool, include_archived=show_all)
    finally:
        pool.close()
    if not projects:
        console.print("[yellow]No projects registered.[/yellow]")
        return
    table = Table(title="Registry projects", box=box.SIMPLE_HEAVY)
    table.add_column("Slug")
    table.add_column("Display name")
    table.add_column("Database")
    table.add_column("Status")
    table.add_column("Created")
    for p in projects:
        table.add_row(
            p["slug"],
            p["display_name"],
            p["database_name"],
            p["status"],
            str(p["created_at"].date()),
        )
    console.print(table)


@runs_app.command("status")
def runs_status(
    ctx: typer.Context,
    run_id: Optional[str] = typer.Option(
        None, "--run-id", help="Check one run (default: every run with a Batch job id)."
    ),
    all_pending: bool = typer.Option(
        False, "--all-pending", help="Only runs still marked 'started'."
    ),
    region: Optional[str] = typer.Option(
        None, "--region", help="AWS region (default: $AWS_REGION)."
    ),
) -> None:
    """Poll AWS Batch for cloud runs and backfill terminal state onto triage.runs.

    A Batch job that died hard (container OOM, spot reclaim) never marks its own run row,
    leaving status='started' forever. This polls describe_jobs: a FAILED job whose run is
    still 'started' is marked 'failed' with the Batch statusReason; everything else is
    reported read-only (a healthy job updates its own row from inside the container).
    """
    from triage.profiles.execution import batch_job_status

    aws_region = region or os.environ.get("AWS_REGION")
    if not aws_region:
        raise typer.BadParameter(
            "no AWS region — pass --region or set AWS_REGION (cloud-profile-spec §5)"
        )
    engine = get_pool(ctx)
    sql = (
        "select run_id, experiment_hash, status, batch_job_id, started_at"
        " from triage.runs where batch_job_id is not null"
    )
    params: Dict[str, Any] = {}
    if run_id:
        sql += " and run_id = %(r)s"
        params["r"] = run_id
    if all_pending:
        sql += " and status = 'started'"
    sql += " order by started_at desc"
    with engine.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        console.print("[yellow]No cloud runs with a Batch job id matched.[/yellow]")
        return

    table = Table(title="Cloud runs — AWS Batch status", box=box.SIMPLE_HEAVY)
    table.add_column("Run")
    table.add_column("Run status")
    table.add_column("Batch job")
    table.add_column("Batch status")
    table.add_column("Reason")
    table.add_column("Backfill")
    for row in rows:
        info = batch_job_status(row["batch_job_id"], region=aws_region)
        backfilled = ""
        if info["status"] == "FAILED" and row["status"] == "started":
            with engine.connection() as conn:
                conn.execute(
                    "update triage.runs set status = 'failed', finished_at = now(),"
                    " error = %(e)s where run_id = %(r)s",
                    {
                        "e": "AWS Batch job failed: "
                        + (info["reason"] or "no statusReason"),
                        "r": row["run_id"],
                    },
                )
            backfilled = "[red]marked failed[/red]"
        table.add_row(
            str(row["run_id"])[:8] + "…",
            row["status"],
            row["batch_job_id"],
            info["status"],
            (info["reason"] or "")[:48],
            backfilled,
        )
    console.print(table)


@postmodel_app.command("crosstabs")
def postmodel_crosstabs(
    ctx: typer.Context,
    model_id: int = typer.Argument(..., callback=natural_number),
    parameter: str = typer.Option(
        "100_abs", "--parameter", "-p", help="Top-k cut (e.g. '100_abs', '10_pct')."
    ),
    split_kind: str = typer.Option(
        "test", "--split", help="Prediction split to analyze."
    ),
    top: int = typer.Option(
        15, "--top", "-n", help="Rows to print (all are persisted)."
    ),
) -> None:
    """Feature means among the top-k vs the rest — what characterizes the list?

    Computes from the model's scored matrix (Parquet, via the storage seam), persists
    long-format rows into ``triage.crosstabs``, prints the most-distinguishing features
    by |log ratio| of means. The dashboard's model card reads the same table.
    """
    from triage.diagnostics import compute_crosstabs

    engine = get_pool(ctx)
    written = compute_crosstabs(
        engine, model_id, parameter=parameter, split_kind=split_kind
    )
    console.print(f"[green]{written} crosstab row(s) persisted.[/green]")
    with engine.connection() as conn:
        rows = conn.execute(
            "select as_of_date, feature, selected_value, rest_value, ratio"
            " from triage.crosstabs"
            " where model_id = %(m)s and parameter = %(p)s and stat = 'mean'"
            "   and ratio is not null and ratio > 0"
            " order by abs(ln(ratio)) desc limit %(n)s",
            {"m": model_id, "p": parameter, "n": top},
        ).fetchall()
    table = Table(
        title=f"Crosstabs — model {model_id} @ {parameter} (top |log ratio| of means)",
        box=box.SIMPLE_HEAVY,
    )
    for col in ("As-of", "Feature", "Selected", "Rest", "Ratio"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["as_of_date"]),
            r["feature"],
            _fmt(r["selected_value"]),
            _fmt(r["rest_value"]),
            _fmt(r["ratio"], 2),
        )
    console.print(table)


@postmodel_app.command("error-tree")
def postmodel_error_tree(
    ctx: typer.Context,
    model_id: int = typer.Argument(..., callback=natural_number),
    parameter: str = typer.Option(
        "100_abs", "--parameter", "-p", help="Top-k cut the errors are defined at."
    ),
    kind: str = typer.Option("both", "--kind", help="fp | fn | both."),
    depth: int = typer.Option(3, "--depth", help="Max tree depth (keep it readable)."),
    min_leaf: int = typer.Option(
        30, "--min-leaf", help="Min samples per leaf (rule support floor)."
    ),
) -> None:
    """Characterize WHERE the model fails: a shallow tree on the errors → rules.

    fp: within the selected top-k, what marks the wrong flags? fn: among the
    passed-over, what marks the missed positives? Rules persist to
    ``triage.error_analysis`` (a diagnostic — never a score modifier). Linear models
    also get per-entity β·x contributions persisted (``individual_importances``).
    """
    from triage.diagnostics import compute_error_analysis

    engine = get_pool(ctx)
    written = compute_error_analysis(
        engine,
        model_id,
        parameter=parameter,
        kind=kind,
        max_depth=depth,
        min_samples_leaf=min_leaf,
    )
    console.print(f"[green]{written} error rule(s) persisted.[/green]")
    with engine.connection() as conn:
        rows = conn.execute(
            "select as_of_date, error_kind, rule, n_matched, n_errors, error_rate"
            " from triage.error_analysis"
            " where model_id = %(m)s and parameter = %(p)s"
            " order by error_rate desc, n_matched desc limit 15",
            {"m": model_id, "p": parameter},
        ).fetchall()
    table = Table(
        title=f"Error rules — model {model_id} @ {parameter}", box=box.SIMPLE_HEAVY
    )
    for col in ("As-of", "Kind", "Rule", "n", "Errors", "Rate"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["as_of_date"]),
            r["error_kind"],
            r["rule"],
            str(r["n_matched"]),
            str(r["n_errors"]),
            _fmt(r["error_rate"], 2),
        )
    console.print(table)


@postmodel_app.command("compare")
def postmodel_compare(
    ctx: typer.Context,
    model_a: int = typer.Argument(..., callback=natural_number),
    model_b: int = typer.Argument(..., callback=natural_number),
    parameter: str = typer.Option(
        "100_abs", "--parameter", "-p", help="Top-k cut for the list comparison."
    ),
    split_kind: str = typer.Option("test", "--split", help="Prediction split."),
) -> None:
    """Do two models flag the same entities? Top-k overlap + Spearman (migration 0016)."""
    engine = get_pool(ctx)
    with engine.connection() as conn:
        rows = conn.execute(
            "select as_of_date, k_a, k_b, n_intersection, jaccard, rank_corr"
            " from triage.list_overlap(%(a)s, %(b)s, %(p)s,"
            "        cast(%(s)s as triage.split_kind), null)"
            " order by as_of_date",
            {"a": model_a, "b": model_b, "p": parameter, "s": split_kind},
        ).fetchall()
    if not rows:
        console.print(
            "[yellow]No shared prediction dates — are both models scored on the same"
            " split?[/yellow]"
        )
        return
    table = Table(
        title=f"List overlap — m{model_a} vs m{model_b} @ {parameter}",
        box=box.SIMPLE_HEAVY,
    )
    for col in ("As-of", "k(a)", "k(b)", "∩", "Jaccard", "Rank corr"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["as_of_date"]),
            str(r["k_a"]),
            str(r["k_b"]),
            str(r["n_intersection"]),
            _fmt(r["jaccard"], 3),
            _fmt(r["rank_corr"], 3),
        )
    console.print(table)


@app.command("archive")
def archive_command(
    ctx: typer.Context,
    experiment_hash: str = typer.Argument(
        ..., help="Experiment to archive (removes it from the GC root set)."
    ),
) -> None:
    """Soft-archive an experiment (ADR-0017). Reversible until a gc sweep."""
    engine = get_pool(ctx)
    archive_experiment(engine, experiment_hash)
    console.print(
        f"[green]Experiment '{experiment_hash}' archived — its artifacts become"
        " collectible by 'triage gc' unless used elsewhere.[/green]"
    )


@app.command("gc")
def gc_command(
    ctx: typer.Context,
    delete: bool = typer.Option(
        False, "--delete", help="Collect dead artifacts' outputs (default: dry run)."
    ),
    do_purge: bool = typer.Option(
        False,
        "--purge",
        help="Also delete the rows of dead collected/failed artifacts.",
    ),
    min_age: int = typer.Option(
        0, "--min-age", help="Only touch artifacts built at least N days ago."
    ),
) -> None:
    """Garbage-collect artifacts unreachable from any root (ADR-0017).

    Roots: non-archived experiments' used artifacts + predicted models.
    Default is a dry run; --delete collects outputs (rows stay, status
    'collected', rebuild on demand); --purge removes dead collected/failed rows.
    """
    engine = get_pool(ctx)
    candidates = gc_candidates(engine, min_age_days=min_age)

    if not candidates:
        console.print("[green]Nothing to collect — all artifacts are live.[/green]")
    else:
        table = Table(title="Collectible artifacts (dead)", box=box.SIMPLE_HEAVY)
        table.add_column("Kind")
        table.add_column("Artifact")
        table.add_column("Output")
        for row in candidates:
            table.add_row(
                str(row["kind"]), row["artifact_id"][:16] + "…", row["output_ref"] or ""
            )
        console.print(table)

    if not delete and not do_purge:
        console.print(
            "[yellow]Dry run — nothing deleted. Use --delete to collect outputs,"
            " --purge to drop dead collected/failed rows.[/yellow]"
        )
        return

    if delete and candidates:
        external = collect(engine, [row["artifact_id"] for row in candidates])
        console.print(
            f"[green]Collected {len(candidates)} artifact(s); in-PG slices deleted.[/green]"
        )
        if external:
            # storage=None → delete_outputs dispatches per-ref by scheme (local FS / s3://).
            result = delete_outputs(None, external)
            console.print(
                f"[green]Deleted {len(result['deleted'])} file-backed output(s)"
                f" via the storage layer.[/green]"
            )
            for ref in result["absent"]:
                console.print(f"[yellow]  already absent: {ref}[/yellow]")

    if do_purge:
        purged = purge(engine, min_age_days=min_age)
        console.print(f"[green]Purged {len(purged)} dead artifact row(s).[/green]")


def execute() -> None:
    app()
