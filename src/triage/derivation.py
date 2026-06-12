"""Derivation hashes: artifact identity over the complete input closure.

Implements ADR-0013 (see docs/derivation-dag.md). An artifact's identity is a
SHA-256 hash over a canonical JSON envelope of its kind, its own config slice,
the derivation ids of its parent artifacts (Merkle DAG), its source-data pins
(ADR-0014), and the engine versions involved.

Pure functions, no database access. The volatile rule (ADR-0014): any source
pin whose version is unknown — or any volatile parent — makes the resulting
derivation non-cacheable. Its id is still computed (with a sentinel standing in
for the missing version) but must never be used for cache hits.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = ["VOLATILE", "Derivation", "canonical_json", "derive"]

# Sentinel standing in for the version of an unpinned source (ADR-0014).
VOLATILE = "__volatile__"


@dataclass(frozen=True)
class Derivation:
    """An artifact identity: stable hex id + cache-hit eligibility."""

    id: str
    cacheable: bool


def _normalize(value: Any) -> Any:
    """Recursively normalize a value into canonical JSON-compatible form.

    Raises TypeError for types without an explicit, stable normalization —
    silently stringifying unknown objects would make hashes depend on repr()
    details.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError(
                f"Non-finite float {value!r} cannot enter a derivation hash;"
                + " normalize it (e.g. to None) before deriving"
            )
        return value
    if isinstance(value, Mapping):
        normalized = {}
        for key in value:
            if not isinstance(key, str):
                raise TypeError(
                    f"Derivation config keys must be strings, got {key!r}"
                    + f" ({type(key).__name__})"
                )
            normalized[key] = _normalize(value[key])
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_normalize(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, datetime.datetime):
        return f"datetime:{value.isoformat()}"
    if isinstance(value, datetime.date):
        return f"date:{value.isoformat()}"
    if isinstance(value, datetime.timedelta):
        return f"timedelta:{value.total_seconds()!r}"
    if isinstance(value, Decimal):
        return f"decimal:{value}"
    if isinstance(value, uuid.UUID):
        return f"uuid:{value}"
    raise TypeError(
        f"Cannot canonicalize {type(value).__name__} for a derivation hash;"
        + " convert it to a JSON-compatible value first"
    )


def canonical_json(value: Any) -> str:
    """Serialize a value to canonical JSON: normalized types, sorted keys."""
    return json.dumps(
        _normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def derive(
    kind: str,
    config: Mapping[str, Any],
    parents: Sequence[Derivation] = (),
    source_pins: Mapping[str, str | None] | None = None,
    engine_versions: Mapping[str, str] | None = None,
) -> Derivation:
    """Compute an artifact's derivation from its complete input closure.

    Args:
        kind: artifact kind, e.g. "cohort", "labels", "matrix", "model".
        config: the artifact's own config slice.
        parents: derivations of upstream artifacts (order-insensitive).
        source_pins: declared source name → version label; None marks an
            unpinned (volatile) source.
        engine_versions: engine name → version, e.g. {"triage-pg": "...",
            "featurizer": "..."}.

    Returns:
        Derivation whose ``cacheable`` is False if any pin is unpinned or any
        parent is itself volatile.
    """
    if not kind or not isinstance(kind, str):
        raise ValueError(f"Derivation kind must be a non-empty string, got {kind!r}")
    pins = dict(source_pins or {})
    cacheable = all(parent.cacheable for parent in parents) and all(
        version is not None for version in pins.values()
    )
    envelope = {
        "kind": kind,
        "config": config,
        "parents": sorted(parent.id for parent in parents),
        "source_pins": {
            name: (VOLATILE if version is None else version)
            for name, version in pins.items()
        },
        "engine_versions": dict(engine_versions or {}),
    }
    digest = hashlib.sha256(canonical_json(envelope).encode("ascii")).hexdigest()
    return Derivation(id=digest, cacheable=cacheable)
