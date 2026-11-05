"""``triage plot``: eight commands, each writing its figure and one summary line.

Exit codes: 0 written; 1 nothing persisted to draw, naming the command that creates it;
2 bad input (an unknown model, model group or Subset, an unsupported extension, the
series cap). Messages are matched without ANSI codes and with whitespace collapsed,
because rich forces colour under GITHUB_ACTIONS and wraps at the terminal width.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from typer.testing import CliRunner

import triage.cli as cli
from tests.test_plots import CUT, Diagnosed, diagnosed  # noqa: F401 — fixture
from tests.test_readers import D1, SUBSET, SUBSET_NAME, group  # noqa: F401 — fixture

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    # rich draws an error in a box: drop its side bars as well as the colour
    return " ".join(_ANSI.sub("", output).replace("│", " ").split())


@pytest.fixture
def plot(db_url, diagnosed, tmp_path, monkeypatch):  # noqa: F811
    """Run ``triage plot …`` against the fixture's database, from ``tmp_path``."""
    monkeypatch.chdir(tmp_path)  # no database.yaml or .env here: DATABASE_URL wins
    runner = CliRunner()

    def run(*args: str):
        return runner.invoke(
            cli.app,
            ["plot", *args],
            env={"DATABASE_URL": db_url},
            catch_exceptions=False,
        )

    return run


def _commands(d: Diagnosed) -> list[tuple[list[str], str, str]]:
    """(arguments, the default file name, a fragment of the summary line)."""
    a, b, g = d.group.model_a, d.group.model_b, d.group.model_group_id
    return [
        (["curve", str(a), "--date", str(D1), "--subset", SUBSET_NAME], f"curve-{a}.svg",
         f"model {a} at {D1} — everyone:"),
        (["curve", str(a), "--date", str(D1), "--compare", str(d.model_c), "--subset",
          SUBSET[:4]],
         f"curve-{a}.svg", f"model {a} and model {d.model_c} at {D1}"),
        (["curve-dates", str(g)], f"curve-dates-{g}.svg", f"3 curve(s) of model group {g}"),
        (["curve-dates", str(g), "--subset", SUBSET_NAME, "--points", "11"],
         f"curve-dates-{g}.svg", f"in Subset {SUBSET_NAME}"),
        (["error-rules", str(a)], f"error-rules-{a}.svg", "2 fp leaves against"),
        (["error-rules", str(a), "-p", CUT, "--kind", "confusion", "--top", "3"],
         f"error-rules-{a}.svg", "1 confusion leaves against"),
        (["leaves", str(g), "-p", CUT], f"leaves-{g}.svg",
         "9 leaves, 3 in the suspicious zone"),
        (["leaves", str(g), "-p", CUT, "--min-support", "101", "--max-error", "0.5"],
         f"leaves-{g}.svg", "9 leaves, 0 in the suspicious zone"),
        (["recurrence", str(g), "-p", CUT, "--kind", "fn"], f"recurrence-{g}.svg",
         "the most recurrent on 3 of 3"),
        (["crosstabs", str(a)], f"crosstabs-{a}.svg", "5 features by mean ratio"),
        (["crosstabs", str(a), "--date", str(D1), "--stat", "nonzero", "--compare",
          str(b), "--top", "3"], f"crosstabs-{a}.svg", "3 features by nonzero rate"),
        (["crosstabs-dates", str(g), "-p", CUT], f"crosstabs-dates-{g}.svg",
         "4 feature(s) by mean ratio across 4 test date(s)"),
        (["crosstabs-dates", str(g), "--stat", "nonzero", "--top", "2"],
         f"crosstabs-dates-{g}.svg", "2 feature(s) by nonzero rate"),
        (["overlap", str(g), str(d.other_group), "-p", CUT],
         f"overlap-{g}-{d.other_group}.svg", "1 shared test date(s) at 10_pct"),
    ]  # fmt: skip


def test_every_command_writes_its_default_file(plot, diagnosed, tmp_path):  # noqa: F811
    for args, name, summary in _commands(diagnosed):
        target = tmp_path / name
        target.unlink(missing_ok=True)
        result = plot(*args)
        assert result.exit_code == 0, (args, result.output)
        plain = _plain(result.output)
        assert f"wrote {name}" in plain, (args, plain)
        assert summary in plain, (args, plain)
        assert ET.parse(target).getroot().tag.endswith("svg"), args


def test_the_extension_picks_the_format(plot, diagnosed, tmp_path):  # noqa: F811
    g = str(diagnosed.group.model_group_id)
    for name, magic in (("out/l.png", b"\x89PNG"), ("out/l.pdf", b"%PDF-")):
        result = plot("leaves", g, "-p", CUT, "-o", name)
        assert result.exit_code == 0, result.output
        assert (tmp_path / name).read_bytes().startswith(magic)
    result = plot("leaves", g, "-p", CUT, "-o", "l.jpg")
    assert result.exit_code == 2
    assert ".svg, .png, .pdf only" in _plain(result.output)
    assert not (tmp_path / "l.jpg").exists()


def test_unknown_ids_and_bad_options_exit_2(plot, diagnosed, tmp_path):  # noqa: F811
    a, g = str(diagnosed.group.model_a), str(diagnosed.group.model_group_id)
    for args, message in (
        (["curve", "999999"], "No model with id 999999"),
        (["crosstabs", a, "--compare", "999998"], "No model with id 999998"),
        (["error-rules", "999999"], "No model with id 999999"),
        (["curve-dates", "999999"], "model group 999999 not found"),
        (["leaves", "999999", "-p", CUT], "model group 999999 not found"),
        (["recurrence", "999999", "-p", CUT], "model group 999999 not found"),
        (["crosstabs-dates", "999999"], "model group 999999 not found"),
        (["overlap", g, "999999", "-p", CUT], "model group 999999 not found"),
        (["curve", a, "--subset", "nowhere"], "No subset matches 'nowhere'"),
        (["crosstabs-dates", g, "--top", "9"], "the cap is 8 series"),
        (["curve", a, "--points", "1"], "points=1 cannot span the population"),
        (["crosstabs", a, "--stat", "median"], "'median' is not a crosstab statistic"),
        (["error-rules", a, "--kind", "tp"], "'tp' is not an error-rule kind"),
        (["curve", a, "--date", "2014-13-01"], "invalid date"),
    ):
        result = plot(*args)
        assert result.exit_code == 2, (args, result.output)
        assert message in _plain(result.output), (args, result.output)
    assert not list(tmp_path.glob("*.svg"))  # nothing was written


def test_nothing_persisted_exits_1_naming_the_command(plot, diagnosed, tmp_path):  # noqa: F811
    c = str(diagnosed.model_c)  # scored, but no error trees or crosstabs
    for args, command in (
        (["error-rules", c], f"triage postmodel error-tree {c}"),
        (["crosstabs", c], f"triage postmodel crosstabs {c}"),
        (
            ["leaves", str(diagnosed.other_group), "-p", CUT],
            "triage postmodel error-tree",
        ),
        (["curve", str(diagnosed.group.model_b), "--date", str(D1)], "triage run"),
    ):
        result = plot(*args)
        assert result.exit_code == 1, (args, result.output)
        assert command in _plain(result.output), (args, result.output)
    assert not list(tmp_path.glob("*.svg"))


def test_the_help_lists_the_eight_commands():
    result = CliRunner().invoke(cli.app, ["plot", "--help"])
    assert result.exit_code == 0
    plain = _plain(result.output)
    for command in (
        "curve",
        "curve-dates",
        "error-rules",
        "leaves",
        "recurrence",
        "crosstabs",
        "crosstabs-dates",
        "overlap",
    ):
        assert f" {command} " in plain, command


def test_the_cli_does_not_import_matplotlib_until_a_plot_is_drawn():
    import subprocess
    import sys

    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, triage.cli;"
            " print('matplotlib' in sys.modules, 'triage.plots' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.split()[-2:] == ["False", "False"], loaded.stdout
    assert Path(cli.__file__).read_text().count("from triage import plots") == 8


def test_a_file_that_cannot_be_written_exits_2_not_1(plot, diagnosed, tmp_path):  # noqa: F811
    """Exit 1 tells the user to run a postmodel command; a write failure must not."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = plot("curve", str(diagnosed.group.model_a), "-o", "locked/c.svg")
    finally:
        locked.chmod(0o700)
    assert result.exit_code == 2, result.output
    assert "could not be drawn or written" in _plain(result.output)
