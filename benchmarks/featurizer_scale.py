#!/usr/bin/env python
"""Scale validation for featurizer's per-as_of_date CTE cost (ADR-0008).

This is **not** a pytest CI test — it is a standalone, reproducible benchmark.
Run it directly:

    uv run python benchmarks/featurizer_scale.py

It spins its **own** throwaway PostgreSQL cluster (via ``pytest_postgresql``'s
``PostgreSQLExecutor`` — a temp datadir torn down on exit), loads synthetic
relational data, renders + executes featurizer's generated SQL, and prints
timing tables. It touches nothing in ``src/`` and never connects to the host
``PG*`` env (which is a broken tunnel in this worktree).

The risk being measured (ADR-0008 §"Consequences"):

    "featurizer re-evaluates aggregation CTEs once per as_of_date with no reuse
     across dates, so its generated SQL must be benchmarked on realistic volumes."

featurizer renders ``select aod.as_of_date, t.* from as_of_dates aod cross join
lateral (<aggregation CTEs>)`` — so the aggregation CTEs are recomputed once per
``as_of_date`` row. The headline question is whether wall-clock grows **linearly**
(expected, acceptable) or **superlinearly** (the real danger) in the number of
as_of_dates.

Axes
----
* PRIMARY:   fix entities/events, vary #as_of_dates {1,5,10,20,40}.
* SECONDARY: fix #as_of_dates, vary #entities {1k,10k,100k}.

For each point we time:

* featurizer SQL *generation* (rendering ``Featurizer.query`` — the planner).
* SQL *execution* — wall-clock of running the generated query to completion
  (materialized into a throwaway TEMP table so we pay the full per-as_of_date
  CTE cost without shipping rows to the client), plus the server-side
  ``EXPLAIN ANALYZE`` "Execution Time".
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import statistics
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import yaml
from pytest_postgresql.executor import PostgreSQLExecutor

# ---------------------------------------------------------------------------
# Synthetic relational schema
# ---------------------------------------------------------------------------
# A target entity table (`clients`) + one child event stream (`visits`), each
# with a temporal_ix (knowledge date). This is the canonical public-policy
# shape: a population of entities, each with a stream of dated events, and we
# want point-in-time features ("activity in the last 30/90/180 days").

CONFIG: dict[str, Any] = {
    "target": "clients",
    "max_depth": 2,
    # 3 intervals × ~5 aggregations × the child numeric/categorical vars gives a
    # non-trivial CTE body (so the per-as_of_date cost is real, not a toy).
    "intervals": ["P30D", "P90D", "P180D"],
    "aggregations": ["count", "sum", "mean", "max", "stddev"],
    "transformations": ["identity"],
    "entities": [
        {
            "alias": "clients",
            "table": "clients",
            "id": "client_id",
            "temporal_ix": "enrolled_on",
            "variables": {
                "region": {"type": "categorical"},
                "baseline_score": {"type": "numeric"},
            },
        },
        {
            "alias": "visits",
            "table": "visits",
            "id": "visit_id",
            "temporal_ix": "visited_on",
            "variables": {
                "amount": {"type": "numeric"},
                "service": {"type": "categorical"},
            },
        },
    ],
    "relationships": [
        {
            "parent": {"entity": "clients", "key": "client_id"},
            "child": {"entity": "visits", "key": "client_id"},
        }
    ],
}

REGIONS = ["north", "south", "east", "west", "central"]
SERVICES = ["intake", "followup", "referral", "closure", "review"]

# Visit dates span this window; as_of_dates are drawn from the tail so the
# rolling intervals (30/90/180d) always have data behind them.
DATA_START = date(2018, 1, 1)
DATA_END = date(2023, 1, 1)
_DATA_SPAN_DAYS = (DATA_END - DATA_START).days


def _load_synthetic_data(
    conn: psycopg.Connection, n_entities: int, events_per_entity: int, seed: int = 7
) -> int:
    """(Re)create + populate clients/visits. Returns the number of visit rows."""
    rng = random.Random(seed)
    with conn.cursor() as cur:
        cur.execute("drop table if exists visits")
        cur.execute("drop table if exists clients")
        cur.execute(
            "create table clients ("
            " client_id bigint primary key,"
            " enrolled_on date not null,"
            " region text,"
            " baseline_score double precision)"
        )
        cur.execute(
            "create table visits ("
            " visit_id bigint primary key,"
            " client_id bigint not null,"
            " visited_on date not null,"
            " amount double precision,"
            " service text)"
        )

        with cur.copy(
            "copy clients (client_id, enrolled_on, region, baseline_score) from stdin"
        ) as copy:
            for cid in range(1, n_entities + 1):
                enrolled = DATA_START + timedelta(days=rng.randint(0, 180))
                copy.write_row(
                    (cid, enrolled, rng.choice(REGIONS), round(rng.uniform(0, 100), 3))
                )

        n_visits = n_entities * events_per_entity
        with cur.copy(
            "copy visits (visit_id, client_id, visited_on, amount, service) from stdin"
        ) as copy:
            vid = 0
            for cid in range(1, n_entities + 1):
                for _ in range(events_per_entity):
                    vid += 1
                    day = rng.randint(0, _DATA_SPAN_DAYS)
                    copy.write_row(
                        (
                            vid,
                            cid,
                            DATA_START + timedelta(days=day),
                            round(rng.uniform(0, 5000), 2),
                            rng.choice(SERVICES),
                        )
                    )

        # Indexes a real deployment would have: the child's (key, temporal_ix)
        # is what the as-of join filters on.
        cur.execute("create index on visits (client_id, visited_on)")
        cur.execute("analyze clients")
        cur.execute("analyze visits")
    conn.commit()
    return n_visits


def _materialize_as_of_dates(conn: psycopg.Connection, n_dates: int) -> None:
    """(Re)create the runtime ``as_of_dates`` table featurizer reads by bare name.

    Dates are monthly, ending at DATA_END, so each has 180+ days of history.
    """
    dates = [DATA_END - timedelta(days=30 * i) for i in range(n_dates)]
    dates.sort()
    with conn.cursor() as cur:
        cur.execute("drop table if exists as_of_dates")
        cur.execute("create table as_of_dates (as_of_date date primary key)")
        with cur.copy("copy as_of_dates (as_of_date) from stdin") as copy:
            for d in dates:
                copy.write_row((d,))
    conn.commit()


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


@dataclass
class Timing:
    n_entities: int
    n_visits: int
    n_dates: int
    gen_seconds: float  # featurizer SQL rendering
    exec_seconds: float  # wall-clock to run the query to completion (CTAS TEMP)
    explain_ms: float  # server-side EXPLAIN ANALYZE Execution Time
    n_cols: int


def _render_query(config_path: str) -> tuple[str, float]:
    """Render featurizer's SQL; return (sql, gen_seconds).

    ``Featurizer.query`` runs the planner + SQL renderer — the per-config cost
    that does *not* depend on #as_of_dates (it produces the same lateral query
    regardless of how many dates the runtime ``as_of_dates`` table holds).
    """
    from featurizer import Featurizer

    t0 = time.perf_counter()
    featurizer = Featurizer(config_path, validate=False)
    sql = featurizer.query
    gen = time.perf_counter() - t0
    return sql, gen


def _time_execution(
    conn: psycopg.Connection, sql: str, statement_timeout_ms: int
) -> tuple[float, float, int]:
    """Execute the query into a TEMP table; return (wall_seconds, explain_ms, n_cols).

    Materializing into ``create temp table ... as`` forces the server to
    evaluate every per-as_of_date CTE and write all output rows — the true cost
    — without paying to ship them to the Python client. We then run
    ``EXPLAIN ANALYZE`` on the same query for the server-reported execution time.
    """
    with conn.cursor() as cur:
        # psycopg types the query as LiteralString to discourage dynamic SQL; this
        # benchmark deliberately interpolates generated SQL, so cast the f-strings.
        cur.execute(
            cast(LiteralString, f"set statement_timeout = {statement_timeout_ms}")
        )

        cur.execute("drop table if exists _bench_out")
        t0 = time.perf_counter()
        cur.execute(cast(LiteralString, f"create temp table _bench_out as {sql}"))
        wall = time.perf_counter() - t0

        cur.execute(
            "select count(*) from information_schema.columns where table_name = '_bench_out'"
        )
        cols_row = cur.fetchone()
        assert cols_row is not None
        n_cols = int(cols_row[0])

        cur.execute(
            cast(LiteralString, f"explain (analyze, timing off, format json) {sql}")
        )
        plan_row = cur.fetchone()
        assert plan_row is not None
        plan = plan_row[0]
        explain_ms = float(plan[0]["Execution Time"])
    conn.rollback()  # drop the temp table / leave no state
    return wall, explain_ms, n_cols


def _write_config(tmpdir: str) -> str:
    path = os.path.join(tmpdir, "bench_config.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(CONFIG, handle, sort_keys=True)
    return path


# ---------------------------------------------------------------------------
# Throwaway PostgreSQL cluster
# ---------------------------------------------------------------------------


@contextmanager
def throwaway_postgres():
    """Start an isolated PostgreSQL cluster in a temp datadir; yield a dsn.

    Uses ``pytest_postgresql``'s executor (same mechanism the test suite uses)
    but driven directly, outside pytest. Torn down (server stopped, datadir
    removed) on exit.
    """
    pg_ctl = shutil.which("pg_ctl")
    if pg_ctl is None:
        raise RuntimeError(
            "pg_ctl not on PATH — install PostgreSQL (e.g. `brew install"
            " postgresql@16`) so the benchmark can spin its own cluster."
        )
    tmp = tempfile.mkdtemp(prefix="featurizer_bench_pg_")
    datadir = os.path.join(tmp, "data")
    os.makedirs(datadir, exist_ok=True)
    # an ephemeral high port to avoid clashing with any running server
    port = random.randint(50000, 59999)
    executor = PostgreSQLExecutor(
        executable=pg_ctl,
        host="127.0.0.1",
        port=port,
        datadir=datadir,
        unixsocketdir=tmp,
        logfile=os.path.join(tmp, "pg.log"),
        startparams="-w",
        dbname="bench",
        user="bench_user",
        password="",
        # generous shared buffers / work_mem so the benchmark measures
        # featurizer's SQL shape, not a starved default cluster.
        postgres_options="-c shared_buffers=256MB -c work_mem=64MB",
    )
    executor.start()
    try:
        # initdb created the bench_user superuser + the default databases, but
        # not our ``bench`` db — create it through the maintenance connection.
        admin = psycopg.connect(
            f"host=127.0.0.1 port={port} user=bench_user dbname=postgres"
        )
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("create database bench")
        admin.close()
        dsn = f"host=127.0.0.1 port={port} user=bench_user dbname=bench"
        yield dsn, str(executor.version)
    finally:
        executor.stop()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


def _run_point(
    conn: psycopg.Connection,
    config_path: str,
    n_entities: int,
    n_visits: int,
    n_dates: int,
    statement_timeout_ms: int,
    repeats: int,
) -> Timing:
    _materialize_as_of_dates(conn, n_dates)
    sql, gen = _render_query(config_path)

    walls: list[float] = []
    explains: list[float] = []
    n_cols = 0
    for _ in range(repeats):
        wall, explain_ms, n_cols = _time_execution(conn, sql, statement_timeout_ms)
        walls.append(wall)
        explains.append(explain_ms)
    return Timing(
        n_entities=n_entities,
        n_visits=n_visits,
        n_dates=n_dates,
        gen_seconds=gen,
        exec_seconds=min(walls),  # best-of to reduce noise
        explain_ms=min(explains),
        n_cols=n_cols,
    )


def _fmt_table(rows: list[list[str]], header: list[str]) -> str:
    widths = [len(h) for h in header]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    line = lambda cells: (
        "| "
        + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))  # noqa: E731
        + " |"
    )
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(header), sep, *(line(r) for r in rows)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="smaller scales for a fast smoke run",
    )
    parser.add_argument(
        "--repeats", type=int, default=2, help="timed repeats per point (best-of)"
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=300,
        help="per-query statement_timeout (seconds)",
    )
    args = parser.parse_args()
    timeout_ms = args.timeout_s * 1000

    if args.quick:
        primary_entities, primary_events = 5_000, 20
        date_axis = [1, 5, 10, 20]
        entity_axis = [(1_000, 20), (10_000, 20)]
        secondary_dates = 10
    else:
        primary_entities, primary_events = 20_000, 30
        date_axis = [1, 5, 10, 20, 40]
        entity_axis = [(1_000, 30), (10_000, 30), (100_000, 30)]
        secondary_dates = 12

    print(
        f"featurizer scale benchmark — repeats={args.repeats}, statement_timeout={args.timeout_s}s\n"
    )

    with throwaway_postgres() as (dsn, pg_version):
        print(f"throwaway PostgreSQL {pg_version} started\n")
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _write_config(tmpdir)
            conn = psycopg.connect(dsn)
            try:
                # ----- PRIMARY: vary #as_of_dates at fixed entities/events -----
                print(
                    f"PRIMARY axis — {primary_entities:,} entities × {primary_events} events/entity, vary #as_of_dates"
                )
                n_visits = _load_synthetic_data(conn, primary_entities, primary_events)
                primary: list[Timing] = []
                for n_dates in date_axis:
                    t = _run_point(
                        conn,
                        config_path,
                        primary_entities,
                        n_visits,
                        n_dates,
                        timeout_ms,
                        args.repeats,
                    )
                    primary.append(t)
                    print(
                        f"  dates={n_dates:>3}  gen={t.gen_seconds:6.3f}s  "
                        f"exec={t.exec_seconds:7.3f}s  "
                        f"explain={t.explain_ms / 1000:7.3f}s  cols={t.n_cols}"
                    )

                # ----- SECONDARY: vary #entities at fixed #as_of_dates -----
                print(
                    f"\nSECONDARY axis — {secondary_dates} as_of_dates, vary #entities"
                )
                secondary: list[Timing] = []
                for n_entities, events in entity_axis:
                    nv = _load_synthetic_data(conn, n_entities, events)
                    t = _run_point(
                        conn,
                        config_path,
                        n_entities,
                        nv,
                        secondary_dates,
                        timeout_ms,
                        args.repeats,
                    )
                    secondary.append(t)
                    print(
                        f"  entities={n_entities:>7,}  visits={nv:>9,}  "
                        f"gen={t.gen_seconds:6.3f}s  exec={t.exec_seconds:7.3f}s  "
                        f"explain={t.explain_ms / 1000:7.3f}s"
                    )
            finally:
                conn.close()

    # ----- report -----
    print(
        "\n\n### PRIMARY: execution time vs #as_of_dates "
        f"({primary_entities:,} entities, {primary[0].n_visits:,} visits)\n"
    )
    rows = []
    base = primary[0]
    for t in primary:
        per_date = t.exec_seconds / t.n_dates
        ratio = t.exec_seconds / base.exec_seconds if base.exec_seconds else 0.0
        rows.append(
            [
                str(t.n_dates),
                f"{t.exec_seconds:.3f}",
                f"{t.explain_ms / 1000:.3f}",
                f"{per_date:.3f}",
                f"{ratio:.2f}x",
            ]
        )
    print(
        _fmt_table(
            rows,
            ["#as_of_dates", "exec (s)", "explain (s)", "s / as_of_date", "vs 1 date"],
        )
    )

    # linearity diagnostic: coefficient of variation of per-as_of_date cost.
    per_date_costs = [t.exec_seconds / t.n_dates for t in primary]
    mean_pd = statistics.mean(per_date_costs)
    cv = (statistics.pstdev(per_date_costs) / mean_pd) if mean_pd else 0.0
    print(
        f"\nper-as_of_date cost: mean={mean_pd:.4f}s, "
        f"coefficient of variation={cv:.1%} "
        f"(low CV ⇒ ~constant per-date cost ⇒ linear total; "
        f"rising per-date cost ⇒ superlinear)"
    )

    print(
        f"\n\n### SECONDARY: execution time vs #entities ({secondary_dates} as_of_dates)\n"
    )
    rows = []
    for t in secondary:
        rows.append(
            [
                f"{t.n_entities:,}",
                f"{t.n_visits:,}",
                f"{t.exec_seconds:.3f}",
                f"{t.explain_ms / 1000:.3f}",
            ]
        )
    print(_fmt_table(rows, ["#entities", "#visits", "exec (s)", "explain (s)"]))


if __name__ == "__main__":
    main()
