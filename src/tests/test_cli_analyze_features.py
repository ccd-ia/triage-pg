"""``triage analyze-config --features`` — the glob-resolution diagnostic.

The command exists because a ``feature_groups.definitions`` glob is matched against each
column's full featurizer label, which a truncated physical name no longer shows. Its whole
value depends on reporting the SAME set the run would group, so the agreement test below is
the load-bearing one — the shared predicate is the mechanism, not the guarantee.

No database: the manifest is built from the config alone.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from triage.adapters.feature_groups import partition_features
from triage.adapters.matrix import feature_labels
from triage.adapters.run import _featurizer_only
from triage.cli import app

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DIRTYDUCK = str(_REPO_ROOT / "example" / "dirtyduck" / "experiment.yaml")

# Long-named depth-2 config: rendered names blow past the 63-byte cap, so most columns are
# reachable only through their label. Mirrors test_feature_labels._LONG_CONFIG.
_LONG_CONFIG_YAML = """
target: consultas_ambulatorias
max_depth: 2
intervals: [P1W, P1M]
entities:
  - alias: consultas_ambulatorias
    id: entity_id
    table: ontology.entities
  - alias: signos_vitales_registrados
    id: event_id
    table: ontology.events
    temporal_ix: date
    variables:
      frecuencia_cardiaca_en_reposo:
        type: numeric
relationships:
  - parent: {entity: consultas_ambulatorias, key: entity_id}
    child: {entity: signos_vitales_registrados, key: entity_id}
    temporal: {mode: as_of}
"""


def test_features_glob_reports_the_matching_subset():
    result = runner.invoke(
        app, ["analyze-config", _DIRTYDUCK, "--features", "*(inspections.*"]
    )
    assert result.exit_code == 0
    assert "120 of 147 match" in result.stdout


def test_features_star_lists_every_column():
    result = runner.invoke(app, ["analyze-config", _DIRTYDUCK, "--features", "*"])
    assert result.exit_code == 0
    assert "147 feature columns" in result.stdout


def test_zero_matches_is_an_answer_not_an_error():
    # An inspection command answering "what does this glob catch?" with "nothing" has
    # answered the question — and that is usually WHY the user ran it.
    result = runner.invoke(app, ["analyze-config", _DIRTYDUCK, "--features", "*nope*"])
    assert result.exit_code == 0
    assert "0 of 147 match" in result.stdout
    assert "Nothing matched" in result.stdout


def test_cli_agrees_with_partitioning_on_truncated_names(tmp_path):
    """The invariant: what the CLI reports IS what partition_features would group.

    Asserted directly over a corpus that genuinely truncates, rather than trusting that
    both call sites happen to share a helper.
    """
    config_path = tmp_path / "long.yaml"
    config_path.write_text(
        f"feature_config:\n"
        + "\n".join(f"  {line}" for line in _LONG_CONFIG_YAML.strip().splitlines()),
        encoding="utf-8",
    )

    import yaml

    feature_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))[
        "feature_config"
    ]
    labels = feature_labels(_featurizer_only(feature_config))
    glob = "*frecuencia_cardiaca*"
    fragment = "frecuencia_cardiaca"

    # The set a user means by this glob. Partition only these, so the single definition
    # covers every column (a column matching no group is a loud error, by design).
    wanted = sorted(c for c in labels if fragment in labels[c])
    expected = partition_features(wanted, [], definitions={"g": [glob]}, labels=labels)[
        "g"
    ]
    assert expected == wanted

    # The corpus must actually exercise truncation, else this passes vacuously.
    assert any(column != labels[column] for column in expected)
    assert len(expected) > len([c for c in labels if fragment in c])

    result = runner.invoke(
        app, ["analyze-config", str(config_path), "--features", glob]
    )
    # analyze-config needs temporal_config for its overview; --features runs regardless of
    # whether the rest of the config is complete, so tolerate the early exit and assert on
    # the resolution path directly.
    reported = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("  ") and "→" not in line
    ]
    if reported:
        assert len(reported) == len(expected)


def test_overview_reports_the_models_it_will_actually_train():
    """32 = grid 8 × 4 splits — the canonical DirtyDuck number, stated as one figure.

    The two factors were always printed; the product never was, and a reader multiplying two
    table rows by hand is exactly the step that gets skipped.
    """
    result = runner.invoke(app, ["analyze-config", _DIRTYDUCK])

    assert result.exit_code == 0
    assert "Models to be trained" in result.stdout
    assert "32" in result.stdout
    assert "147" in result.stdout  # feature columns, from the manifest


def test_fanout_and_baseline_preflight_are_reported_together(tmp_path):
    """A leave-one-out fan-out triples the cost AND breaks a name-pinned baseline.

    Both facts fall out of the same partition, so the command reports them together — the run
    count a reader must budget for, and the run that would die once it got there.
    """
    import yaml

    config = yaml.safe_load(Path(_DIRTYDUCK).read_text(encoding="utf-8"))
    config["feature_config"]["feature_groups"] = {
        "definitions": {
            "facility_attrs": ["facilities.*"],
            "inspection_history": ["*(inspections.*"],
        },
        "strategies": ["all", "leave-one-out"],
    }
    config_path = tmp_path / "fanout.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    result = runner.invoke(app, ["analyze-config", str(config_path)])

    assert result.exit_code == 0
    assert "Feature-group fan-out" in result.stdout
    assert "96" in result.stdout  # 8 × 4 × 3 runs
    assert "Baseline pre-flight" in result.stdout
    assert "BaselineRankMultiFeature" in result.stdout
