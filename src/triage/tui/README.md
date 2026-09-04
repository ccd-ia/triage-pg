# triage tui

The terminal cockpit for a triage-pg project, built on
[lynkeus](https://github.com/nanounanue/lynkeus), the shared Textual shell
(lynkeus ADR-0001). The shell owns the six standard screens, the keys and the
theme; this package owns the three adapters that feed them and the project
screens that follow (Experiments, Leaderboard, Audition).

```bash
just tui                       # uv run triage tui
uv run triage tui --poll 10    # refresh the active screen every 10 s (0 = off)
```

The database is resolved exactly as for every other verb (`--dbfile` ›
`database.yaml` › `DATABASE_URL` › `PG*`). Credentials never appear here.

## Screens

| Tab | Reads | Notes |
|---|---|---|
| 1 Status | `triage.runs`, `experiments`, `artifacts`, `pg_class` sizes, `results_schema_versions` | pending work is derived from queries: runs still `started` after 6 h, artifacts stuck `building`, a leaderboard matview missing completed runs, experiments without a run |
| 2 Runs | `triage.runs` + `run_progress` + `run_artifacts`; `LISTEN run_progress` (ADR-0021) with a 5 s poll of the view when LISTEN is unavailable | `o` opens the run in the dashboard (`$TRIAGE_DASHBOARD_URL`, default the local `just serve`) |
| 3 Data | the catalog (`pg_class`, `pg_attribute`, `pg_indexes`, `pg_stat_user_tables`) | `4` opens the selected relation in Query |
| 4 Query | any read-only statement; the nine `triage` views are the saved queries | your own saved queries live under `~/.local/state/triage/tui/<project>/` |
| 5 Actions | `just --dump` (cwd) + the typer commands of `triage.cli` | `gc`, `archive`, `db downgrade`, `project drop` and the `*-clean` recipes are confirmed first; a verb with required arguments (`triage run CONFIG`) is prompted for them rather than left to exit 2 |
| 6 Experiments | `experiment_summary` | enter drills into the experiment's runs |
| 7 Leaderboard | the `leaderboard` matview | `R` refreshes it (through the CLI) |
| 8 Audition | `audition_distances` / `audition` | sparklines of distance-from-best per model group |

No business logic lives here (ADR-0012): every number is a `SELECT` over the
same views the dashboard and the CLI read, and every mutation is the CLI run
as a subprocess whose exit code becomes the action's state.

## Headless twins

The same adapters print for a terminal or an agent:

```bash
triage status --json
triage runs list --limit 10 --json
triage runs show <run-id-prefix> --json
triage runs tail <run-id-prefix> --json        # one JSON object per event
triage query "select * from triage.run_summary limit 5" --json
triage actions list --json
triage actions run "just test"
triage actions run triage -- --version
```

`triage runs status` (the AWS Batch backfill) is unchanged and sits beside them.

## Layout

```
triage/tui/
  adapters.py   TriageStatus · TriageRuns · TriageActions · SAVED_QUERIES
  screens.py    Experiments · Leaderboard · Audition
  app.py        build_app(db_url) → lynkeus.app.ShellApp
tests/tui_tests/  adapters over the dashboard's seeded experiment; the CLI verbs; Pilot runs
```
