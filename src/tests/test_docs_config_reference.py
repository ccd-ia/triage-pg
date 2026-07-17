"""Guard the published configuration reference against validator drift.

The site page ``docs-site/src/content/docs/reference/configuration.md`` documents
every top-level ``experiment.yaml`` key. Its source of truth is
``_KNOWN_TOP_LEVEL_KEYS`` in :mod:`triage.adapters.run` (what the validator
actually reads). If someone adds a config key to the validator but forgets the
docs, this test fails until the reference is updated — the reference cannot
silently rot.
"""

from __future__ import annotations

from pathlib import Path

from triage.adapters.run import _KNOWN_TOP_LEVEL_KEYS

_CONFIG_REFERENCE = (
    Path(__file__).resolve().parents[2]
    / "docs-site"
    / "src"
    / "content"
    / "docs"
    / "reference"
    / "configuration.md"
)


def test_config_reference_documents_every_known_top_level_key():
    assert (
        _CONFIG_REFERENCE.exists()
    ), f"configuration reference missing at {_CONFIG_REFERENCE}"
    text = _CONFIG_REFERENCE.read_text(encoding="utf-8")
    undocumented = sorted(key for key in _KNOWN_TOP_LEVEL_KEYS if key not in text)
    assert not undocumented, (
        "the configuration reference is missing these top-level keys "
        f"(add them to {_CONFIG_REFERENCE.name}): {undocumented}"
    )
