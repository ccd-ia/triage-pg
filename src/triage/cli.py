from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import typer
import yaml
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from sqlalchemy.engine.url import URL
from sqlalchemy.orm import sessionmaker

from triage.adapters.forward import predict_forward
from triage.adapters.retrain import retrain_and_predict
from triage.adapters.run import run_experiment
from triage.artifacts import (
    archive_experiment,
    collect,
    delete_outputs,
    gc_candidates,
    purge,
)
from triage.component.architect.entity_date_table_generators import (
    EntityDateTableGenerator,
)
from triage.component.architect.feature_generators import FeatureGenerator
from triage.component.audition import AuditionRunner
from triage.component.catwalk.model_trainers import flatten_grid_config
from triage.component.catwalk.storage import CSVMatrixStore, ProjectStorage, Store
from triage.component.results_schema import (
    TriageRun,
    TriageRunStatus,
    db_history,
    downgrade_db,
    stamp_db,
    upgrade_db,
)
from triage.component.timechop import Timechop
from triage.component.timechop.plotting import visualize_chops
from triage.experiments import (
    CONFIG_VERSION,
    SingleThreadedExperiment,
)
from triage.logging import configure_logging, get_logger
from triage.sources import (
    bump_source,
    check_drift,
    get_source,
    list_sources,
    register_source,
)
from triage.util.conf import load_query_if_needed
from triage.util.db import create_engine

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

DEFAULT_DATABASE_FILE = pathlib.Path("database.yaml")
DEFAULT_SETUP_FILE = pathlib.Path("experiment.py")

MATRIX_STORAGE_MAP = {"csv": CSVMatrixStore}


@dataclass
class CLIState:
    db_url: str
    setup_path: Optional[pathlib.Path]


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
    store = Store.factory(path)
    with store.open("r") as fd:
        return fd.read()


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
        dotenv_loaded = load_dotenv(dotenv_path=env_path, override=True)
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
            url_components = {
                "drivername": "postgresql+psycopg",  # Use psycopg (version 3)
                "host": pg_host,
                "username": pg_user or "postgres",
                "database": pg_database,
                "port": int(pg_port) if pg_port else 5432,
            }

            # Only add password if it's not None
            if pg_password:
                url_components["password"] = pg_password

            url = URL.create(**url_components)
            # Use render_as_string with hide_password=False to preserve the actual password
            return url.render_as_string(hide_password=False)

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
        url = URL.create(
            "postgresql+psycopg",  # Use psycopg (version 3)
            host=config["host"],
            username=config["user"],
            database=config["db"],
            password=config["pass"],
            port=config["port"],
        )
        # Use render_as_string with hide_password=False to preserve the actual password
        return url.render_as_string(hide_password=False)
    except KeyError as exc:
        raise typer.BadParameter(
            "database.yaml is missing required keys: host, user, pass, port, db"
        ) from exc


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


def get_engine(ctx: typer.Context):
    state = get_state(ctx)
    return create_engine(state.db_url)


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
    store = Store.factory(config_path)
    with store.open("r") as fd:
        return yaml.full_load(fd) or {}


def prepare_experiment(
    ctx: typer.Context,
    config: Dict[str, Any],
    project_path: pathlib.Path,
    *,
    replace: bool,
    materialize_fromobjs: bool,
    features_ignore_cohort: bool,
    matrix_storage_format: str,
    profile: bool,
    save_predictions: bool,
    skip_validation: bool,
    additional_bigtrain_classnames: Optional[Iterable[str]],
) -> tuple[Dict[str, Any], Any]:
    engine = get_engine(ctx)
    matrix_storage_class = MATRIX_STORAGE_MAP[matrix_storage_format]
    kwargs = dict(
        config=config,
        db_engine=engine,
        project_path=str(project_path),
        replace=replace,
        materialize_subquery_fromobjs=materialize_fromobjs,
        features_ignore_cohort=features_ignore_cohort,
        matrix_storage_class=matrix_storage_class,
        profile=profile,
        save_predictions=save_predictions,
        skip_validation=skip_validation,
        additional_bigtrain_classnames=list(additional_bigtrain_classnames or []),
    )
    return kwargs, engine


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

    db_url = resolve_db_url(dbfile)
    setup_path = resolve_setup_path(setup)
    if setup_path:
        load_setup_module(setup_path)
    ctx.obj = CLIState(db_url=db_url, setup_path=setup_path)
    logger.info("Using database %s", db_url)
    if setup_path:
        logger.info("Setup module: %s", setup_path)


@app.command("featuretest")
def feature_test(
    ctx: typer.Context,
    feature_config_file: str = typer.Argument(
        ..., help="Feature config YAML containing feature_aggregations."
    ),
    as_of_date: datetime = typer.Argument(
        ..., callback=parse_date, help="Date (YYYY-MM-DD) to build features for."
    ),
) -> None:
    engine = get_engine(ctx)
    full_config = load_yaml_from_store(feature_config_file)
    feature_config = full_config["feature_aggregations"]
    cohort_config = load_query_if_needed(full_config.get("cohort_config"))

    state_table = "features_test.test_cohort"
    if cohort_config:
        EntityDateTableGenerator(
            entity_date_table_name=state_table,
            db_engine=engine,
            query=cohort_config["query"],
            replace=True,
        ).generate_entity_date_table(as_of_dates=[as_of_date])

    FeatureGenerator(engine, "features_test").create_features_before_imputation(
        feature_aggregation_config=feature_config,
        feature_dates=[as_of_date],
        state_table=state_table,
    )
    console.print(
        f"[green]Feature test completed for {as_of_date.date()}[/green]",
        justify="left",
    )


@app.command("run")
def run_command(
    ctx: typer.Context,
    config: str = typer.Argument(
        ...,
        help="Greenfield experiment config YAML (problem_type, temporal_config,"
        " cohort_config, label_config, feature_config, grid_config, sources).",
    ),
    project_path: pathlib.Path = typer.Option(
        pathlib.Path.cwd(),
        "--project-path",
        help="Directory or URI to store Parquet matrices + joblib models.",
    ),
    random_seed: int = typer.Option(
        0,
        "--random-seed",
        help="Deterministic seed stored on the run + passed to models.",
    ),
    profile: str = typer.Option(
        "local", "--profile", help="Run profile: 'local' or 'cloud'."
    ),
    cache_policy: str = typer.Option(
        "exact", "--cache-policy", help="Artifact cache policy: 'exact' or 'logical'."
    ),
) -> None:
    """Run a greenfield experiment end-to-end (cohort→labels→matrix→model→eval, ADR-0012).

    The single CLI entry point onto ``triage.adapters.run.run_experiment`` — the headless
    core that drives the whole artifact-DAG pipeline from one config.
    """
    engine = get_engine(ctx)
    config_data = load_experiment_config(config)
    result = run_experiment(
        engine,
        config_data,
        storage_dir=str(project_path),
        random_seed=random_seed,
        profile=profile,
        cache_policy=cache_policy,
    )
    console.print(
        f"[green]Run {str(result.run_id)[:8]}… completed:[/green]"
        f" {result.num_models} model(s), {result.num_predictions} prediction(s),"
        f" {result.num_evaluations} evaluation(s)."
    )
    console.print(
        f"[cyan]Experiment:[/cyan] {result.experiment_hash[:12]}…"
        f"  [cyan]storage:[/cyan] {project_path}"
    )


@app.command("experiment")
def experiment_command(
    ctx: typer.Context,
    config: str = typer.Argument(..., help="Experiment configuration file."),
    project_path: pathlib.Path = typer.Option(
        pathlib.Path.cwd(),
        "--project-path",
        help="Directory or URI to store matrices and models.",
    ),
    n_db_processes: int = typer.Option(
        1, "--n-db-processes", callback=natural_number, help="DB worker count."
    ),
    n_processes: int = typer.Option(
        1, "--n-processes", callback=natural_number, help="Model worker count."
    ),
    n_bigtrain_processes: int = typer.Option(
        1,
        "--n-bigtrain-processes",
        callback=natural_number,
        help="Worker count for large estimators.",
    ),
    add_bigtrain_class: Optional[List[str]] = typer.Option(
        None,
        "--add-bigtrain-class",
        help=(
            "Additional classifier paths to train with the big-model batch. Use multiple times for multiple classes."
        ),
    ),
    matrix_format: str = typer.Option(
        "csv",
        "--matrix-format",
        help="Matrix storage backend.",
        show_choices=list(MATRIX_STORAGE_MAP.keys()),
    ),
    replace: bool = typer.Option(
        False, "--replace", help="Replace existing artifacts."
    ),
    validate: bool = typer.Option(
        True, "--validate/--no-validate", help="Validate config before running."
    ),
    validate_only: bool = typer.Option(
        False, "--validate-only", help="Only validate the config and exit."
    ),
    profile: bool = typer.Option(
        False,
        "--profile",
        help="Profile experiment runtime with cProfile (implies serialized run).",
    ),
    materialize_fromobjs: bool = typer.Option(
        True,
        "--materialize-fromobjs/--no-materialize-fromobjs",
        help="Create tables for feature from-objects subqueries.",
    ),
    save_predictions: bool = typer.Option(
        True,
        "--save-predictions/--no-save-predictions",
        help="Persist individual predictions to the database.",
    ),
    features_ignore_cohort: bool = typer.Option(
        False,
        "--features-ignore-cohort",
        help="Store features independently of the cohort definition.",
    ),
    show_timechop: bool = typer.Option(
        False,
        "--show-timechop",
        help="Render the timechop diagram to <project-path>/images.",
    ),
) -> None:
    matrix_format = matrix_format.lower()
    if matrix_format not in MATRIX_STORAGE_MAP:
        raise typer.BadParameter(
            f"Unsupported matrix format '{matrix_format}'. Available: {', '.join(MATRIX_STORAGE_MAP.keys())}"
        )
    config_data = load_experiment_config(config)
    kwargs, _engine = prepare_experiment(
        ctx,
        config_data,
        project_path,
        replace=replace,
        materialize_fromobjs=materialize_fromobjs,
        features_ignore_cohort=features_ignore_cohort,
        matrix_storage_format=matrix_format,
        profile=profile,
        save_predictions=save_predictions,
        skip_validation=not validate,
        additional_bigtrain_classnames=add_bigtrain_class,
    )

    console.print(
        f"[cyan]Triage config version:[/cyan] {config_data.get('config_version', CONFIG_VERSION)}"
    )
    console.print(f"[cyan]Project path:[/cyan] {project_path}")

    if n_db_processes > 1 or n_processes > 1 or n_bigtrain_processes > 1:
        console.print(
            "[yellow]Multi-process execution was removed (ADR-0005); "
            "running in-process. The --n-*-processes flags are ignored.[/yellow]"
        )

    try:
        experiment = SingleThreadedExperiment(**kwargs)
    except Exception as exc:
        console.print(f"[red]Failed to initialize experiment: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if validate_only:
        console.print("[yellow]Validating configuration...[/yellow]")
        experiment.validate()
        console.print("[green]Validation completed.[/green]")
        return

    if show_timechop:
        experiment_name = pathlib.Path(config).stem
        project_storage = ProjectStorage(str(project_path))
        target_store = project_storage.get_store(["images"], f"{experiment_name}.png")
        with target_store.open("wb") as fd:
            visualize_chops(experiment.chopper, save_target=fd)
        console.print("[green]Timechop image saved.[/green]")
        return

    console.print("[yellow]Running experiment...[/yellow]")
    experiment.run()
    console.print("[green]Experiment completed successfully.[/green]")


@app.command("audition")
def audition_command(
    ctx: typer.Context,
    config: str = typer.Option(
        "audition_config.yaml",
        "--config",
        "-c",
        help="Audition configuration file.",
    ),
    validate: bool = typer.Option(
        True, "--validate/--no-validate", help="Validate configuration first."
    ),
    validate_only: bool = typer.Option(
        False, "--validate-only", help="Only validate audition config."
    ),
    directory: Optional[pathlib.Path] = typer.Option(
        None, "--directory", "-d", help="Directory to store generated plots."
    ),
) -> None:
    engine = get_engine(ctx)
    config_data = load_yaml_from_store(config)
    runner = AuditionRunner(config_data, engine, str(directory) if directory else None)
    if validate or validate_only:
        runner.validate()
        if validate_only:
            console.print("[green]Audition config validates successfully.[/green]")
            return
    runner.run()
    console.print("[green]Audition completed.[/green]")


@app.command("retrainpredict")
def retrain_predict_command(
    ctx: typer.Context,
    model_group_id: int = typer.Argument(..., callback=natural_number),
    prediction_date: datetime = typer.Argument(..., callback=parse_date),
    project_path: pathlib.Path = typer.Option(
        pathlib.Path.cwd(), "--project-path", help="Artifact storage path."
    ),
) -> None:
    engine = get_engine(ctx)
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
    engine = get_engine(ctx)
    predict_forward(
        engine,
        model_id,
        as_of_date.date(),
        storage_dir=str(project_path),
    )
    console.print("[green]Prediction list generated.[/green]")


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
    upgrade_db(revision=revision, dburl=get_state(ctx).db_url)
    console.print("[green]Database upgraded.[/green]")


@db_app.command("downgrade")
def db_downgrade(
    ctx: typer.Context,
    revision: str = typer.Option(
        "-1", "--revision", "-r", help="Schema revision to downgrade to."
    ),
) -> None:
    downgrade_db(revision=revision, dburl=get_state(ctx).db_url)
    console.print("[green]Database downgraded.[/green]")


@db_app.command("stamp")
def db_stamp(
    ctx: typer.Context,
    revision: str = typer.Argument(..., help="Revision to stamp the DB with."),
) -> None:
    stamp_db(revision=revision, dburl=get_state(ctx).db_url)
    console.print("[green]Database stamped.[/green]")


@db_app.command("history")
def db_history_command(ctx: typer.Context) -> None:
    db_history(dburl=get_state(ctx).db_url)


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
) -> None:
    """Declare a source table (idempotent)."""
    engine = get_engine(ctx)
    register_source(
        engine,
        name,
        relation,
        knowledge_date_column=knowledge_date_column,
        description=description,
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
    engine = get_engine(ctx)
    label = bump_source(engine, name, version_label)
    console.print(f"[green]Source '{name}' pinned at '{label}'.[/green]")


@source_app.command("list")
def source_list(ctx: typer.Context) -> None:
    """List sources with their current pins (unpinned sources are volatile)."""
    engine = get_engine(ctx)
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
    engine = get_engine(ctx)
    source = get_source(engine, name)
    if source is None:
        console.print(f"[red]Source '{name}' is not registered.[/red]")
        raise typer.Exit(code=1)
    drifted = check_drift(engine, name)
    body = "\n".join(f"[cyan]{key}:[/cyan] {value}" for key, value in source.items())
    if drifted:
        body += "\n[red]DRIFT: data changed since the current pin.[/red]"
    console.print(Panel.fit(body, title=f"Source: {name}"))


@app.command("archive")
def archive_command(
    ctx: typer.Context,
    experiment_hash: str = typer.Argument(
        ..., help="Experiment to archive (removes it from the GC root set)."
    ),
) -> None:
    """Soft-archive an experiment (ADR-0017). Reversible until a gc sweep."""
    engine = get_engine(ctx)
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
    engine = get_engine(ctx)
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
            result = delete_outputs(external)
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
