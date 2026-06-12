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
import importlib.metadata
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "VOLATILE",
    "Derivation",
    "as_uuid",
    "canonical_json",
    "derive",
    "engine_versions_for",
]

# Sentinel standing in for the version of an unpinned source (ADR-0014).
VOLATILE = "__volatile__"

# Namespace for mapping derivation ids onto UUIDs (matrix_uuid, ADR-0015).
_TRIAGE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "triage-pg.ccd-ia")


@dataclass(frozen=True)
class Derivation:
    """An artifact identity: strict + logical hex ids and cache eligibility.

    ``id`` is the strict identity (includes engine versions, ADR-0016);
    ``logical_id`` is the engine-version-free Merkle chain used only by the
    opt-in fallback reuse policy — it hashes over parents' logical ids so
    engine drift anywhere upstream does not break fallback matching.
    """

    id: str
    logical_id: str
    cacheable: bool


def as_uuid(derivation_id: str) -> uuid.UUID:
    """Deterministically map a derivation id to a UUID.

    Used where a uuid-typed key is kept for storage-naming convenience
    (``matrices.matrix_uuid`` := ``as_uuid(artifact_id)``, ADR-0015).
    """
    return uuid.uuid5(_TRIAGE_NAMESPACE, derivation_id)


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
    base = {
        "kind": kind,
        "config": config,
        "source_pins": {
            name: (VOLATILE if version is None else version)
            for name, version in pins.items()
        },
    }
    strict_envelope = {
        **base,
        "parents": sorted(parent.id for parent in parents),
        "engine_versions": dict(engine_versions or {}),
    }
    logical_envelope = {
        **base,
        "parents": sorted(parent.logical_id for parent in parents),
    }
    return Derivation(
        id=_hash(strict_envelope),
        logical_id=_hash(logical_envelope),
        cacheable=cacheable,
    )


def _hash(envelope: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(envelope).encode("ascii")).hexdigest()


# Per-kind engine relevance (ADR-0016): each node kind hashes exactly the
# "compilers" that determine its output bytes. PostgreSQL and Python are
# runtimes, deliberately excluded — recorded at the run level instead.
def engine_versions_for(
    kind: str, estimator_class_path: str | None = None
) -> dict[str, str]:
    """The engine versions that enter a node of ``kind``'s identity.

    triage-pg enters every kind; featurizer additionally enters feature
    groups; the estimator's distribution (resolved from its class path)
    additionally enters models.
    """
    versions = {"triage-pg": _distribution_version("triage-pg", "triage")}
    if kind == "feature_group":
        versions["featurizer"] = _distribution_version("featurizer")
    if kind == "model":
        if not estimator_class_path:
            raise ValueError(
                "engine_versions_for('model') requires the estimator class path"
                + " — the estimator library's version enters model identity (ADR-0016)"
            )
        top_module = estimator_class_path.split(".")[0]
        distributions = importlib.metadata.packages_distributions().get(top_module)
        if not distributions:
            raise ValueError(
                f"Cannot resolve a distribution for estimator module {top_module!r}"
                + f" (from {estimator_class_path!r}); is it installed?"
            )
        name = distributions[0]
        versions[name] = importlib.metadata.version(name)
    return versions


def _distribution_version(*candidates: str) -> str:
    for name in candidates:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise importlib.metadata.PackageNotFoundError(
        f"None of the distributions {candidates!r} is installed — cannot pin"
        + " its version into artifact identity (ADR-0016)"
    )
