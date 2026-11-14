"""The threshold-free identity of an error-tree rule, shared by every tree kind."""

from __future__ import annotations

from typing import Any


def signature_of(conditions: list[dict[str, Any]]) -> str:
    """A rule's threshold-free identity: its distinct ``feature op`` pairs, sorted.

    Sorting makes it independent of the order the tree split on the features, and dropping
    the thresholds lets the same rule match on another date, where they move. A path that
    splits one feature twice the same way (``a <= 3 AND a <= 1``) has the signature of one
    split. The root-only tree has no conditions and the signature ``''``.
    """
    return "|".join(sorted({f"{c['feature']}{c['op']}" for c in conditions}))
