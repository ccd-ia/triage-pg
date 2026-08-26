"""Guard: no triage relation may be named in SQL without its schema (the v1.1.2 defect class).

``triage db upgrade`` once wrote the alembic stamp as an unqualified
``results_schema_versions``. PostgreSQL resolved it through the *connecting role's*
``search_path`` — which starts with ``public`` in every database we own, and started with
``raw, clean, ontology`` in the guest PG17 RDS where it was found. The table landed in the
host project's ``raw`` schema. Every test we had passed.

That is the shape this guard exists to catch: SQL that is correct in our databases and wrong
in someone else's. A 2026-08-26 tree-wide audit found no remaining instances; this keeps it
that way, because the defect is invisible to every other test in the suite — it needs a
hostile ``search_path`` to show itself, and our fixtures do not have one.

**Only real SQL is scanned.** Python comments, docstrings and log messages routinely contain
prose like "read directly from predictions" or "sweep the declared strategies into subsets";
an audit regex run over raw file text reports eleven such phrases on this tree and would make
the guard useless. String literals are extracted with :mod:`ast`, docstrings dropped, and only
literals that actually open a SQL statement are examined — then their ``--`` and ``/* */``
comments are stripped too.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "triage"

# Names that legitimately resolve through search_path, or are not triage's to qualify.
_SCHEMA_PREFIXES = (
    "triage.",
    "registry.",
    "public.",
    "pg_",
    "pg_catalog.",
    "information_schema.",
)

# A literal is SQL if it opens a statement. Substring-matching "select" would pull in prose.
_SQL_OPENERS = re.compile(
    r"^\s*(?:--[^\n]*\n\s*)*"
    r"(with|select|insert\s+into|update|delete\s+from|create|drop|alter|refresh|comment\s+on|do|grant|revoke|truncate)\b",
    re.I,
)

_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)

_UNQUALIFIED = re.compile(
    r"(?<![.\w])(from|join|into|update)\s+"
    rf"(?!(?:{'|'.join(re.escape(p) for p in _SCHEMA_PREFIXES)}))"
    r"([a-z_][a-z_0-9]*)\b",
    re.I,
)


def _python_files() -> list[pathlib.Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in str(p)]


def _all_files() -> list[pathlib.Path]:
    return _python_files() + list(_SRC.rglob("*.sql"))


def _triage_relations() -> set[str]:
    """Every relation anyone in the tree qualifies as ``triage.x`` / ``registry.x``.

    Derived rather than hardcoded: a table added by a future migration joins the vocabulary
    the moment any code refers to it properly, so the guard widens on its own.
    """
    names: set[str] = set()
    for path in _all_files():
        names |= set(
            re.findall(
                r"\b(?:triage|registry)\.([a-z_][a-z_0-9]{4,})\b",
                path.read_text(encoding="utf-8"),
            )
        )
    # Python module paths (triage.adapters, triage.logging, …) are not relations.
    modules = {p.stem for p in _SRC.glob("*.py")} | {
        p.name for p in _SRC.iterdir() if p.is_dir()
    }
    names -= modules
    names.add(
        "results_schema_versions"
    )  # the original defect, even if nothing qualifies it
    return names


def _sql_literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """``(lineno, sql)`` for every string literal in ``path`` that opens a SQL statement.

    Docstrings are skipped: a bare string expression statement is documentation, and the
    module docstrings in this package are full of SQL-shaped prose.
    """
    if path.suffix == ".sql":
        return [(1, path.read_text(encoding="utf-8"))]

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
            and _SQL_OPENERS.match(node.value)
        ):
            out.append((node.lineno, node.value))
    return out


def _violations() -> list[str]:
    relations = _triage_relations()
    found: list[str] = []
    for path in _all_files():
        for lineno, sql in _sql_literals(path):
            for match in _UNQUALIFIED.finditer(_SQL_COMMENT.sub(" ", sql)):
                relation = match.group(2).lower()
                if relation in relations:
                    found.append(
                        f"{path.relative_to(_SRC)}:{lineno} — "
                        f"`{match.group(1)} {match.group(2)}` is unqualified; "
                        f"write `triage.{match.group(2)}`"
                    )
    return found


def test_no_triage_relation_is_named_without_its_schema():
    """The guard itself. A hit here would be invisible until someone runs on a guest DB."""
    violations = _violations()

    assert not violations, (
        "unqualified triage relation(s) in SQL — these resolve through the connecting "
        "role's search_path and will land in the wrong schema on a host database "
        "(the v1.1.2 stamp-table bug):\n  " + "\n  ".join(violations)
    )


def test_the_guard_can_actually_fail(tmp_path):
    """Red-verify the guard in-process: it must flag a planted unqualified relation.

    A green guard proves nothing on its own — this one is a regex over an AST walk, and every
    step of that (the docstring filter, the SQL-opener filter, the comment stripper) is a place
    it could silently stop matching anything at all.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        '"""A docstring mentioning select from predictions must NOT trip it."""\n'
        "import logging\n"
        'logging.info("scored rows from predictions")  # prose, not SQL\n'
        'GOOD = "select 1 from triage.predictions"\n'
        'BAD = "select 1 from predictions"\n',
        encoding="utf-8",
    )

    literals = _sql_literals(planted)
    hits = [
        match.group(2)
        for _, sql in literals
        for match in _UNQUALIFIED.finditer(_SQL_COMMENT.sub(" ", sql))
        if match.group(2).lower() in {"predictions"}
    ]

    assert hits == ["predictions"], (
        f"guard must flag exactly the unqualified one, got {hits} "
        f"from {len(literals)} SQL literal(s)"
    )


def test_prose_and_deliberate_bare_names_are_not_flagged():
    """The false-positive floor, pinned to the cases the 2026-08-26 audit had to reason about.

    Each of these is correct as written, and a guard that flagged them would be turned off:

    * SQL comments — ``-- read directly from predictions`` (migration 0011/0015)
    * a bare quoted identifier — ``version_table.py`` probes the pre-fix stamp *deliberately*,
      hunting wherever search_path put it; that is the v1.1.2 fix, not the bug
    * a user-declared relation — ``s.relation::regclass`` resolves the source relation the
      operator wrote in their own config
    """
    scanned = _SQL_COMMENT.sub(
        " ", "select 1 from triage.labels -- joined from labels for the rollup"
    )
    assert not [
        m for m in _UNQUALIFIED.finditer(scanned) if m.group(2).lower() == "labels"
    ]

    # A bare quoted identifier is not a SQL statement, so it is never scanned.
    assert _SQL_OPENERS.match('"results_schema_versions"') is None

    # `s.relation` is schema-qualified-looking and, more to the point, not a triage relation.
    assert "relation" not in _triage_relations()


@pytest.mark.parametrize(
    "catalog, filter_column",
    [("pg_matviews", "schemaname"), ("information_schema.tables", "table_schema")],
)
def test_catalog_lookups_filter_by_schema(catalog, filter_column):
    """The second shape: a catalog query with no schema filter is the same ambiguity.

    ``select … from pg_matviews`` without ``schemaname`` will happily return a host project's
    objects. Every current call site filters; this keeps a new one from forgetting.
    """
    offenders: list[str] = []
    for path in _all_files():
        for lineno, sql in _sql_literals(path):
            body = _SQL_COMMENT.sub(" ", sql)
            if catalog in body.lower() and filter_column not in body.lower():
                offenders.append(f"{path.relative_to(_SRC)}:{lineno}")

    assert not offenders, (
        f"`{catalog}` queried without a `{filter_column}` filter — on a shared cluster this "
        f"returns another project's objects:\n  " + "\n  ".join(offenders)
    )
