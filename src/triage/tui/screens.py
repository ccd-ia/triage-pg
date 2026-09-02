"""triage-pg's own screens for the lynkeus shell: Experiments, Leaderboard, Audition.

Each one is a query over a view the dashboard already reads (ADR-0012):
``experiment_summary``, the ``leaderboard`` matview and
``audition_distances`` / ``audition``. Sparklines only; anything richer opens
the dashboard (``o``). The one write, refreshing the matview, runs the CLI
as a subprocess (``triage leaderboard --refresh``) and shows its exit code.
"""

from __future__ import annotations

import subprocess
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from lynkeus import PgSource, RunState
from lynkeus.screens import ShellScreen
from lynkeus.text import GLYPHS, STYLES, age, clip, count, spark
from lynkeus.widgets import Panel, colour
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Static

from triage.tui.adapters import TriageActions

_LAST_STATUS = {
    "started": RunState.RUNNING,
    "completed": RunState.SUCCEEDED,
    "failed": RunState.FAILED,
}


@dataclass
class ExperimentContext:
    """What the three screens share: the experiment and the metric being looked at.

    Set by whichever screen the user last moved in; read by the others when
    they activate, so Leaderboard and Audition follow Experiments.
    """

    experiment_hash: str | None = None
    metric: str | None = None
    parameter: str | None = None
    names: dict[str, str] = field(default_factory=dict)

    @property
    def pair(self) -> str:
        """``metric@parameter`` for titles."""
        if not self.metric:
            return "—"
        return f"{self.metric}@{self.parameter}" if self.parameter else self.metric


def _experiments(source: PgSource) -> list[dict[str, Any]]:
    return source.rows(
        "select experiment_hash, name, description, author, problem_type, task_framing,"
        "       created_at, n_runs, last_started_at, last_status, n_model_groups,"
        "       n_models, n_splits, n_features, base_rate, cohort_size, archived_at"
        " from triage.experiment_summary"
        " order by last_started_at desc nulls last, created_at desc"
    )


def _pairs(source: PgSource, experiment_hash: str) -> list[tuple[str, str]]:
    rows = source.rows(
        "select distinct e.metric, e.parameter from triage.evaluations e"
        " join triage.models m on m.model_id = e.model_id"
        " join triage.runs r on r.run_id = m.run_id"
        " where r.experiment_hash = %(h)s and e.split_kind = 'test'"
        " order by e.metric, e.parameter",
        {"h": experiment_hash},
    )
    return [(r["metric"], r["parameter"]) for r in rows]


class _ContextScreen(ShellScreen):
    """Shared plumbing: pick an experiment and a metric pair, cycle them with keys."""

    def __init__(
        self, source: PgSource, context: ExperimentContext, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.source = source
        self.context = context
        self.pairs: list[tuple[str, str]] = []

    def ensure_context(self) -> None:
        """Default to the most recent experiment and its first metric pair."""
        if self.context.experiment_hash is None:
            rows = _experiments(self.source)
            if rows:
                self.context.experiment_hash = rows[0]["experiment_hash"]
                self.context.names = {r["experiment_hash"]: r["name"] for r in rows}
        if self.context.experiment_hash is None:
            return
        self.pairs = _pairs(self.source, self.context.experiment_hash)
        if (
            self.pairs
            and (self.context.metric, self.context.parameter) not in self.pairs
        ):
            self.context.metric, self.context.parameter = self.pairs[0]

    def cycle_pair(self, step: int = 1) -> None:
        """``m``: the next (metric, parameter) evaluated for this experiment."""
        if not self.pairs:
            return
        index = -1
        if self.context.metric is not None:
            current = (self.context.metric, self.context.parameter or "")
            index = self.pairs.index(current) if current in self.pairs else -1
        self.context.metric, self.context.parameter = self.pairs[
            (index + step) % len(self.pairs)
        ]
        self.refresh_data()

    def cycle_experiment(self, step: int = 1) -> None:
        """``x``: the next experiment, newest first."""
        hashes = [r["experiment_hash"] for r in _experiments(self.source)]
        if not hashes:
            return
        current = self.context.experiment_hash
        index = hashes.index(current) if current in hashes else -1
        self.context.experiment_hash = hashes[(index + step) % len(hashes)]
        self.context.metric = None
        self.refresh_data()

    def experiment_label(self) -> str:
        """``name · hash8`` of the current experiment."""
        h = self.context.experiment_hash or ""
        name = self.context.names.get(h, "")
        return f"{name} · {h[:8]}".strip(" ·") if h else "no experiment"

    def action_cycle_pair(self) -> None:
        """Key binding target."""
        self.cycle_pair()

    def action_cycle_experiment(self) -> None:
        """Key binding target."""
        self.cycle_experiment()


# ------------------------------------------------------------ experiments


class ExperimentsScreen(ShellScreen):
    """One row per prediction problem (ADR-0022); enter drills into its runs."""

    SLUG = "experiments"
    TITLE = "Experiments"
    KEYS = (("enter", "runs of this experiment"), ("o", "open"), ("y", "copy as json"))
    PRIMARY = "#experiments-table"

    BINDINGS = [
        Binding("o", "open", "open", show=False),
        Binding("y", "copy", "copy as json", show=False),
        Binding("escape", "focus_table", "back", show=False),
    ]

    DEFAULT_CSS = """
    ExperimentsScreen Horizontal { height: 1fr; }
    ExperimentsScreen #experiments-left { width: 1fr; }
    ExperimentsScreen #experiments-left DataTable { height: 1fr; }
    ExperimentsScreen #experiments-left Input {
        height: 1; border: none; padding: 0; background: $surface;
    }
    ExperimentsScreen #experiments-right { width: 42; padding: 0 0 0 1; }
    ExperimentsScreen #experiment-detail { height: auto; }
    """

    def __init__(
        self,
        source: PgSource,
        context: ExperimentContext,
        dashboard_url: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.source = source
        self.context = context
        self.dashboard_url = dashboard_url.rstrip("/")
        self.rows: list[dict[str, Any]] = []
        self.filter_text = ""
        self.selected: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        """Table + filter on the left, the selected experiment on the right."""
        with Horizontal():
            with Panel("experiments", id="experiments-left", classes="-fill"):
                table = DataTable(
                    cursor_type="row", zebra_stripes=False, id="experiments-table"
                )
                table.add_columns(
                    "",
                    "name",
                    "problem",
                    "framing",
                    "runs",
                    "models",
                    "base rate",
                    "last",
                )
                yield table
                yield Input(
                    placeholder="/ filter", classes="filter", id="experiments-filter"
                )
            with Panel("experiment", id="experiments-right", classes="-fill"):
                yield Static(
                    "[$text-muted]select an experiment[/]", id="experiment-detail"
                )
        yield self.keys_bar()

    def refresh_data(self) -> None:
        """Reload ``experiment_summary``."""
        self.load(
            lambda: _experiments(self.source), self.show_rows, group="experiments"
        )

    def show_rows(self, rows: list[dict[str, Any]]) -> None:
        """Render the table."""
        self.rows = rows
        self.context.names = {r["experiment_hash"]: r["name"] for r in rows}
        table = self.query_one("#experiments-table", DataTable)
        table.clear()
        now = self.now()
        muted = colour(self.app, "text-muted")
        for row in rows:
            haystack = f"{row['name']} {row['experiment_hash']} {row['problem_type']}"
            if self.filter_text and self.filter_text.lower() not in haystack.lower():
                continue
            state = _LAST_STATUS.get(row["last_status"] or "", RunState.QUEUED)
            glyph = Text(GLYPHS[state], style=colour(self.app, STYLES[state]))
            if row["archived_at"]:
                glyph = Text("▫", style=colour(self.app, "text-disabled"))
            base_rate = (
                "" if row["base_rate"] is None else f"{float(row['base_rate']):.3f}"
            )
            table.add_row(
                glyph,
                clip(row["name"], 20),
                Text(row["problem_type"] or "", style=muted),
                Text(row["task_framing"] or "", style=muted),
                str(row["n_runs"] or 0),
                str(row["n_models"] or 0),
                base_rate,
                Text(age(row["last_started_at"], now), style=muted),
                key=row["experiment_hash"],
            )
        if self.selected is None and rows:
            self.select(rows[0]["experiment_hash"])
        elif self.selected is not None:
            self.select(self.selected["experiment_hash"], force=True)

    def select(self, experiment_hash: str, force: bool = False) -> None:
        """Make ``experiment_hash`` the shared context and show its detail."""
        if (
            not force
            and self.selected
            and self.selected["experiment_hash"] == experiment_hash
        ):
            return
        row = next(
            (r for r in self.rows if r["experiment_hash"] == experiment_hash), None
        )
        if row is None:
            return
        self.selected = row
        if self.context.experiment_hash != experiment_hash:
            self.context.experiment_hash = experiment_hash
            self.context.metric = None
        self.load(
            lambda: self._series(experiment_hash), self.show_detail, group="detail"
        )

    def _series(self, experiment_hash: str) -> dict[str, list[float]]:
        rows = self.source.rows(
            "with latest as (select run_id from triage.runs where experiment_hash = %(h)s"
            "                order by started_at desc limit 1)"
            " select b.as_of_date, b.base_rate, c.n_entities"
            " from triage.label_base_rate b"
            " join latest using (run_id)"
            " left join triage.cohort_profile c using (run_id, as_of_date)"
            " order by b.as_of_date",
            {"h": experiment_hash},
        )
        return {
            "base rate": [float(r["base_rate"] or 0) for r in rows],
            "cohort": [float(r["n_entities"] or 0) for r in rows],
        }

    def show_detail(self, series: dict[str, list[float]]) -> None:
        """Render the right-hand panel."""
        row = self.selected
        if row is None:
            return
        lines = [f"[b]{row['name']}[/b]  [$text-muted]{row['experiment_hash'][:12]}[/]"]
        if row["description"]:
            lines.append(f"[$text-muted]{clip(row['description'], 44)}[/]")
        facts = {
            "problem": f"{row['problem_type'] or ''} · {row['task_framing'] or ''}".strip(
                " ·"
            ),
            "author": row["author"] or "",
            "created": (
                row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else ""
            ),
            "runs": f"{row['n_runs'] or 0} · last {row['last_status'] or '—'}",
            "models": f"{row['n_models'] or 0} in {row['n_model_groups'] or 0} groups",
            "splits": f"{row['n_splits'] or 0} · {row['n_features'] or 0} features",
            "cohort": count(row["cohort_size"]),
            "archived": (
                row["archived_at"].strftime("%Y-%m-%d") if row["archived_at"] else ""
            ),
        }
        for key, value in facts.items():
            if value:
                lines.append(f"[$text-muted]{key:<10}[/] {value}")
        for name, values in series.items():
            if values:
                lines.append("")
                lines.append(
                    f"[$text-muted]{name:<10}[/] [$primary]{spark(values, 30)}[/]"
                )
                lines.append(
                    f"[$text-muted]{'':<10} {len(values)} as-of dates · last {values[-1]:g}[/]"
                )
        self.query_one("#experiment-detail", Static).update("\n".join(lines))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Selection follows the cursor."""
        if event.row_key is not None and event.row_key.value is not None:
            self.select(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter: the Runs tab, filtered to this experiment's runs."""
        if self.selected is None:
            return
        from lynkeus.app import ShellApp

        name = self.selected["name"]
        app = self.app
        assert isinstance(app, ShellApp)
        app.action_tab_slug("runs")
        app.screen_for("runs").query_one("#runs-filter", Input).value = name

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter the table."""
        self.filter_text = event.value
        self.show_rows(self.rows)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Back to the table."""
        self.action_focus_table()

    def action_focus_table(self) -> None:
        """Focus the table."""
        self.query_one("#experiments-table", DataTable).focus()

    def action_open(self) -> None:
        """Open the experiment in the dashboard."""
        if self.selected is None:
            return
        webbrowser.open(
            f"{self.dashboard_url}/experiments/{self.selected['experiment_hash']}"
        )

    def action_copy(self) -> None:
        """Copy the selected row as JSON."""
        if self.selected is not None:
            self.copy_json(self.selected)

    def sql_for_selection(self) -> str | None:
        """The summary row."""
        if self.selected is None:
            return None
        return (
            "select *\nfrom triage.experiment_summary\n"
            f"where experiment_hash = '{self.selected['experiment_hash']}';"
        )

    def poll(self) -> None:
        """Runs change; the summary follows."""
        self.refresh_data()


# ------------------------------------------------------------- leaderboard


class LeaderboardScreen(_ContextScreen):
    """The ``triage.leaderboard`` matview, one row per model group, sparkline over as-of dates."""

    SLUG = "leaderboard"
    TITLE = "Leaderboard"
    KEYS = (
        ("m", "metric"),
        ("x", "experiment"),
        ("R", "refresh matview"),
        ("y", "copy as json"),
    )
    PRIMARY = "#leaderboard-table"

    BINDINGS = [
        Binding("m", "cycle_pair", "metric", show=False),
        Binding("x", "cycle_experiment", "experiment", show=False),
        Binding("R", "refresh_matview", "refresh matview", show=False),
        Binding("y", "copy", "copy as json", show=False),
    ]

    DEFAULT_CSS = """
    LeaderboardScreen Vertical { height: 1fr; }
    LeaderboardScreen #leaderboard-head { height: 1; padding: 0 1; }
    LeaderboardScreen #leaderboard-table { height: 1fr; }
    LeaderboardScreen #leaderboard-note { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(
        self,
        source: PgSource,
        context: ExperimentContext,
        actions: TriageActions,
        **kwargs: Any,
    ) -> None:
        super().__init__(source, context, **kwargs)
        self.actions = actions
        self.groups: list[dict[str, Any]] = []
        self.process: subprocess.Popen[str] | None = None
        self.refresh_note: str | None = None

    def compose(self) -> ComposeResult:
        """Header, table, note."""
        with Vertical():
            yield Static("", id="leaderboard-head")
            table = DataTable(
                cursor_type="row", zebra_stripes=False, id="leaderboard-table"
            )
            table.add_columns(
                "group",
                "model type",
                "n",
                "mean",
                "min",
                "max",
                "over as-of dates",
                "last",
            )
            yield table
            yield Static("", id="leaderboard-note")
        yield self.keys_bar()

    def refresh_data(self) -> None:
        """Reload the matview for the current experiment + metric."""
        self.load(self._fetch, self.show_groups, group="leaderboard")

    def _fetch(self) -> tuple[list[dict[str, Any]], bool]:
        self.ensure_context()
        if self.context.experiment_hash is None or not self.context.metric:
            return [], True
        populated = self.source.rows(
            "select relispopulated as p from pg_class where oid = 'triage.leaderboard'::regclass"
        )[0]["p"]
        if not populated:
            return [], False
        rows = self.source.rows(
            "select l.model_group_id, l.model_type, l.as_of_date, l.value"
            " from triage.leaderboard l"
            " where l.experiment_hash = %(h)s and l.metric = %(m)s and l.parameter = %(p)s"
            "   and l.split_kind = 'test' and l.value is not null"
            " order by l.model_group_id, l.as_of_date",
            {
                "h": self.context.experiment_hash,
                "m": self.context.metric,
                "p": self.context.parameter,
            },
        )
        by_group: dict[int, dict[str, Any]] = {}
        for row in rows:
            entry = by_group.setdefault(
                int(row["model_group_id"]),
                {
                    "model_group_id": int(row["model_group_id"]),
                    "model_type": row["model_type"],
                    "values": [],
                },
            )
            entry["values"].append(float(row["value"]))
        groups = list(by_group.values())
        for entry in groups:
            values = entry["values"]
            entry["mean"] = sum(values) / len(values)
            entry["min"], entry["max"], entry["last"] = (
                min(values),
                max(values),
                values[-1],
            )
        groups.sort(key=lambda e: e["mean"], reverse=True)
        return groups, True

    def show_groups(self, result: tuple[list[dict[str, Any]], bool]) -> None:
        """Render."""
        groups, populated = result
        self.groups = groups
        head = f"[b]{self.experiment_label()}[/b]  [$text-muted]metric[/] {self.context.pair}"
        if self.pairs:
            head += f"  [$text-muted]({len(self.pairs)} evaluated · m cycles)[/]"
        self.query_one("#leaderboard-head", Static).update(head)
        table = self.query_one("#leaderboard-table", DataTable)
        table.clear()
        muted = colour(self.app, "text-muted")
        primary = colour(self.app, "primary")
        for entry in groups:
            table.add_row(
                str(entry["model_group_id"]),
                clip(str(entry["model_type"]).rsplit(".", 1)[-1], 28),
                str(len(entry["values"])),
                f"{entry['mean']:.4f}",
                Text(f"{entry['min']:.4f}", style=muted),
                Text(f"{entry['max']:.4f}", style=muted),
                Text(spark(entry["values"], 24), style=primary),
                f"{entry['last']:.4f}",
                key=str(entry["model_group_id"]),
            )
        note = self.query_one("#leaderboard-note", Static)
        if self.refresh_note:
            note.update(self.refresh_note)
            self.refresh_note = None
        elif not populated:
            note.update(
                "[$warning]matview not populated[/] — R runs `triage leaderboard --refresh`"
            )
        elif not groups:
            note.update("[$text-muted]no rows for this experiment and metric[/]")
        else:
            note.update(
                f"[$text-muted]{len(groups)} model groups · test split · full cohort · "
                "sorted by mean · R refreshes the matview through the CLI[/]"
            )

    def action_refresh_matview(self) -> None:
        """Run ``triage leaderboard --refresh <hash>`` as a subprocess, then reload."""
        if self.context.experiment_hash is None:
            return
        if self.process is not None and self.process.poll() is None:
            self.app.notify("refresh already running", timeout=2)
            return
        note = self.query_one("#leaderboard-note", Static)
        note.update(
            "[$primary]●[/] refreshing the matview through `triage leaderboard --refresh`…"
        )
        experiment_hash = self.context.experiment_hash
        try:
            self.process = self.actions.run(
                "triage", ["leaderboard", "--refresh", "--limit", "1", experiment_hash]
            )
        except Exception as exc:  # noqa: BLE001 — shown, not hidden
            self.report_error("refresh", exc)
            return
        process = self.process

        def wait() -> int:
            if process.stdout is not None:
                process.stdout.read()
            return process.wait()

        self.load(wait, self.refreshed, group="refresh")

    def refreshed(self, code: int) -> None:
        """Exit code → note, then reload."""
        if code == 0:
            self.refresh_note = "[$success]✓[/] matview refreshed (exit 0)"
        else:
            self.refresh_note = f"[$error]✗[/] refresh exited {code} — see `triage leaderboard --refresh`"
        self.refresh_data()

    def action_copy(self) -> None:
        """Copy the table as JSON."""
        self.copy_json(self.groups)

    def sql_for_selection(self) -> str | None:
        """The matview rows behind the table."""
        if self.context.experiment_hash is None:
            return None
        return (
            "select model_group_id, model_type, as_of_date, value\n"
            "from   triage.leaderboard\n"
            f"where  experiment_hash = '{self.context.experiment_hash}'\n"
            f"  and  metric = '{self.context.metric}' and parameter = '{self.context.parameter}'\n"
            "  and  split_kind = 'test'\norder by model_group_id, as_of_date;"
        )

    def poll(self) -> None:
        """The matview only moves on refresh; nothing to poll."""


# ---------------------------------------------------------------- audition


class AuditionScreen(_ContextScreen):
    """Distance-from-best per model group across as-of dates (``audition_distances``)."""

    SLUG = "audition"
    TITLE = "Audition"
    KEYS = (("m", "metric"), ("x", "experiment"), ("y", "copy as json"))
    PRIMARY = "#audition-table"

    BINDINGS = [
        Binding("m", "cycle_pair", "metric", show=False),
        Binding("x", "cycle_experiment", "experiment", show=False),
        Binding("y", "copy", "copy as json", show=False),
    ]

    DEFAULT_CSS = """
    AuditionScreen Vertical { height: 1fr; }
    AuditionScreen #audition-head { height: 1; padding: 0 1; }
    AuditionScreen #audition-table { height: 1fr; }
    AuditionScreen #audition-note { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(
        self, source: PgSource, context: ExperimentContext, **kwargs: Any
    ) -> None:
        super().__init__(source, context, **kwargs)
        self.groups: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        """Header, table, note."""
        with Vertical():
            yield Static("", id="audition-head")
            table = DataTable(
                cursor_type="row", zebra_stripes=False, id="audition-table"
            )
            table.add_columns(
                "group",
                "model type",
                "splits",
                "avg value",
                "avg dist",
                "max regret",
                "regret next",
                "distance over time",
            )
            yield table
            yield Static("", id="audition-note")
        yield self.keys_bar()

    def refresh_data(self) -> None:
        """Reload the audition views for the current experiment + metric."""
        self.load(self._fetch, self.show_groups, group="audition")

    def _fetch(self) -> list[dict[str, Any]]:
        self.ensure_context()
        if self.context.experiment_hash is None or not self.context.metric:
            return []
        params = {
            "h": self.context.experiment_hash,
            "m": self.context.metric,
            "p": self.context.parameter,
        }
        summary = self.source.rows(
            "select a.model_group_id, mg.model_type, a.n_splits_evaluated, a.avg_value,"
            "       a.avg_distance_from_best, a.max_regret, a.avg_regret_next_time"
            " from triage.audition a"
            " join triage.model_groups mg on mg.model_group_id = a.model_group_id"
            " where a.experiment_hash = %(h)s and a.metric = %(m)s and a.parameter = %(p)s"
            " order by a.avg_distance_from_best, a.avg_value desc",
            params,
        )
        distances: dict[int, list[float]] = defaultdict(list)
        for row in self.source.rows(
            "select model_group_id, as_of_date, dist_from_best_case"
            " from triage.audition_distances"
            " where experiment_hash = %(h)s and metric = %(m)s and parameter = %(p)s"
            " order by model_group_id, as_of_date",
            params,
        ):
            distances[int(row["model_group_id"])].append(
                float(row["dist_from_best_case"])
            )
        groups = []
        for row in summary:
            entry = dict(row)
            entry["model_group_id"] = int(row["model_group_id"])
            entry["distances"] = distances.get(entry["model_group_id"], [])
            groups.append(entry)
        return groups

    def show_groups(self, groups: list[dict[str, Any]]) -> None:
        """Render."""
        self.groups = groups
        head = f"[b]{self.experiment_label()}[/b]  [$text-muted]metric[/] {self.context.pair}"
        if self.pairs:
            head += f"  [$text-muted]({len(self.pairs)} evaluated · m cycles)[/]"
        self.query_one("#audition-head", Static).update(head)
        table = self.query_one("#audition-table", DataTable)
        table.clear()
        muted = colour(self.app, "text-muted")
        accent = colour(self.app, "accent")

        def num(value: Any) -> str:
            return "" if value is None else f"{float(value):.4f}"

        for entry in groups:
            table.add_row(
                str(entry["model_group_id"]),
                clip(str(entry["model_type"]).rsplit(".", 1)[-1], 26),
                str(entry["n_splits_evaluated"]),
                num(entry["avg_value"]),
                num(entry["avg_distance_from_best"]),
                Text(num(entry["max_regret"]), style=muted),
                Text(num(entry["avg_regret_next_time"]), style=muted),
                Text(spark(entry["distances"], 24), style=accent),
                key=str(entry["model_group_id"]),
            )
        note = self.query_one("#audition-note", Static)
        if groups:
            note.update(
                "[$text-muted]best first · avg distance from the best group per as-of"
                " date · sparkline: that distance over time (flat and low = steady)[/]"
            )
        else:
            note.update(
                "[$text-muted]no test evaluations for this experiment and metric[/]"
            )

    def action_copy(self) -> None:
        """Copy the table as JSON."""
        self.copy_json(self.groups)

    def sql_for_selection(self) -> str | None:
        """The distances behind the sparklines."""
        if self.context.experiment_hash is None:
            return None
        return (
            "select model_group_id, as_of_date, raw_value, best_value, dist_from_best_case\n"
            "from   triage.audition_distances\n"
            f"where  experiment_hash = '{self.context.experiment_hash}'\n"
            f"  and  metric = '{self.context.metric}' and parameter = '{self.context.parameter}'\n"
            "order by model_group_id, as_of_date;"
        )

    def poll(self) -> None:
        """Evaluations land at run end; r reloads."""


def project_screens(
    source: PgSource, dashboard_url: str, actions: TriageActions | None = None
) -> list[ShellScreen]:
    """The screens ``triage tui`` adds after the standard five (tabs 6, 7, 8)."""
    context = ExperimentContext()
    return [
        ExperimentsScreen(source, context, dashboard_url),
        LeaderboardScreen(source, context, actions or TriageActions()),
        AuditionScreen(source, context),
    ]
