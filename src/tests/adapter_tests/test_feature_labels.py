"""``feature_labels`` — the column → label map behind explicit feature groups (ADR-0023).

No database anywhere in this module, deliberately: featurizer's planner (and therefore its
feature manifest) is built in ``Featurizer.__init__``, and triage-pg always declares one-hot
vocabularies (adapter-spec §4), so the map is fully determined by the config. That is what
makes it identical on the matrix cache-hit path, where featurizer never runs.
"""

from pathlib import Path

import pytest
import yaml

from triage.adapters.feature_groups import partition_features
from triage.adapters.matrix import feature_labels
from triage.adapters.run import _featurizer_only

_DIRTYDUCK = (
    Path(__file__).resolve().parents[3] / "example" / "dirtyduck" / "experiment.yaml"
)

# A depth-2 config over long-named entities: the rendered names blow well past
# PostgreSQL's 63-byte cap, so the manifest is the only way to reach most of them.
_LONG_CONFIG = {
    "target": "consultas_ambulatorias",
    "max_depth": 2,
    "intervals": ["P1W", "P1M"],
    "entities": [
        {
            "alias": "consultas_ambulatorias",
            "id": "entity_id",
            "table": "ontology.entities",
        },
        {
            "alias": "signos_vitales_registrados",
            "id": "event_id",
            "table": "ontology.events",
            "temporal_ix": "date",
            "variables": {"frecuencia_cardiaca_en_reposo": {"type": "numeric"}},
        },
    ],
    "relationships": [
        {
            "parent": {"entity": "consultas_ambulatorias", "key": "entity_id"},
            "child": {"entity": "signos_vitales_registrados", "key": "entity_id"},
            "temporal": {"mode": "as_of"},
        }
    ],
}


@pytest.fixture(scope="module")
def dirtyduck_labels():
    config = yaml.safe_load(_DIRTYDUCK.read_text(encoding="utf-8"))
    return feature_labels(_featurizer_only(config["feature_config"]))


@pytest.fixture(scope="module")
def long_labels():
    return feature_labels(_LONG_CONFIG)


def test_dirtyduck_manifest_matches_the_published_feature_count(dirtyduck_labels):
    # 147 is the count the tutorials publish; a drift here means the config or the engine
    # changed shape, which would move feature-group identity too.
    assert len(dirtyduck_labels) == 147


def test_dirtyduck_has_no_truncated_columns(dirtyduck_labels):
    # Why this bug survived: every tutorial config is under the cap, so the repo's own test
    # matrix cannot see the failure. Asserted so the blind spot is documented, not assumed.
    assert [c for c, label in dirtyduck_labels.items() if c != label] == []


def test_keys_are_unquoted_physical_column_names(long_labels):
    # ``pg_identifier`` returns a QUOTED identifier; the manifest reports it unquoted, which
    # is what matrix feature_names carry. A mismatch here would make every lookup miss and
    # silently fall back to identity — the exact silent degradation this work removes.
    assert not [c for c in long_labels if c.startswith('"') or c.endswith('"')]


def test_no_key_exceeds_the_postgres_identifier_cap(long_labels):
    assert not [c for c in long_labels if len(c.encode()) > 63]


def test_long_config_actually_truncates(long_labels):
    # Guards the fixture: if this config stopped truncating, the tests below would pass
    # vacuously.
    truncated = {c: label for c, label in long_labels.items() if c != label}
    assert len(truncated) > 100
    column, label = next(iter(truncated.items()))
    assert len(column.encode()) == 63
    assert len(label.encode()) > 63
    assert "~" in column


def test_labels_reach_columns_the_physical_name_cannot(long_labels):
    fragment = "frecuencia_cardiaca"
    by_label = [c for c, label in long_labels.items() if fragment in label]
    by_column = [c for c in long_labels if fragment in c]

    # The gap IS the bug: these columns are unreachable by any writable glob without labels.
    assert len(by_label) > len(by_column)

    groups = partition_features(
        sorted(by_label),
        [],
        definitions={"cardiac": [f"*{fragment}*"]},
        labels=long_labels,
    )
    assert groups["cardiac"] == sorted(by_label)

    with pytest.raises(ValueError, match="matches no feature_groups"):
        partition_features(
            sorted(by_label), [], definitions={"cardiac": [f"*{fragment}*"]}
        )


def test_map_is_deterministic_across_calls():
    # Cold and warm runs must partition identically; the map is rebuilt each time, so its
    # stability for a given config is what underwrites that.
    assert feature_labels(_LONG_CONFIG) == feature_labels(_LONG_CONFIG)


def test_invalid_config_raises_rather_than_returning_a_partial_map(monkeypatch):
    # Fail-loud: a half-built map would silently degrade grouping to physical names.
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(Exception):
        feature_labels({"target": "nope", "entities": []})
