"""One ``{as_of_date}`` spelling works in every config block (#11).

``cohort.py`` and ``labels.py`` substitute the date as a **quoted literal**, so every shipped
cohort and label example writes ``{as_of_date}::date``. ``subsets.py`` substituted it **bare**, so
that same spelling rendered ``2022-10-01::date`` — parsed by PostgreSQL as the integer expression
``2022 - 10 - 1`` and rejected with ``cannot cast type integer to date``. Meanwhile the module's
own docstring and the Chicago 311 tutorial taught the *quoted* spelling, which was correct here
and wrong in a cohort. Two meanings, one token, nothing saying so.

These tests **execute** each rendering rather than only comparing strings: the failure mode is a
PostgreSQL parse error, so a test that stops at the rendered text would not have caught it.
"""

from __future__ import annotations

import pytest

from triage.adapters.placeholders import render_as_of_date
from triage.adapters.subsets import register_subsets

AS_OF = "2014-01-01"

SPELLINGS = {
    # The cohort/label spelling — a hard error before this fix.
    "cast": "select entity_id from subset_src where created_date < {as_of_date}::date",
    # The spelling this module's docstring and the Chicago 311 tutorial teach.
    "quoted": "select entity_id from subset_src where created_date < '{as_of_date}'",
    # The reporter's own workaround.
    "date_literal": (
        "select entity_id from subset_src where created_date < date '{as_of_date}'"
    ),
}


@pytest.mark.parametrize("name", sorted(SPELLINGS))
def test_every_spelling_renders_to_the_same_sql(name: str) -> None:
    """All three forms collapse to one quoted literal, so they cannot mean different things."""
    rendered = render_as_of_date(SPELLINGS[name], AS_OF)
    assert "'2014-01-01'" in rendered
    assert "2014-01-01::date" not in rendered, (
        "a bare literal is the bug, not a spelling"
    )
    assert "''2014-01-01''" not in rendered, (
        "the quote pair must be stripped, not doubled"
    )


def test_an_unrelated_quoted_literal_is_untouched() -> None:
    """The regex is anchored on the placeholder, so other literals survive verbatim."""
    query = (
        "select entity_id from subset_src"
        " where status = 'OPEN' and created_date < {as_of_date}::date"
    )
    rendered = render_as_of_date(query, AS_OF)
    assert "status = 'OPEN'" in rendered
    assert "created_date < '2014-01-01'::date" in rendered


def test_a_repeated_placeholder_renders_every_occurrence() -> None:
    query = (
        "select entity_id from subset_src"
        " where created_date >= {as_of_date}::date - interval '1 month'"
        "   and created_date <  '{as_of_date}'"
    )
    rendered = render_as_of_date(query, AS_OF)
    assert rendered.count("'2014-01-01'") == 2
    assert "interval '1 month'" in rendered


@pytest.mark.parametrize("name", sorted(SPELLINGS))
def test_every_spelling_executes_against_postgres(
    name: str, db_pool_greenfield
) -> None:
    """The decisive check: PostgreSQL accepts each rendering and returns the right rows."""
    engine = db_pool_greenfield
    with engine.connection() as conn:
        conn.execute(
            "create table if not exists subset_src"
            " (entity_id bigint, created_date date)"
        )
        conn.execute("delete from subset_src")
        conn.execute(
            "insert into subset_src (entity_id, created_date) values"
            " (1, date '2013-06-01'), (2, date '2013-12-31'), (3, date '2014-06-01')"
        )

    registered = register_subsets(
        engine,
        [{"name": f"slice_{name}", "query": SPELLINGS[name]}],
        [AS_OF],
    )
    assert len(registered) == 1

    with engine.connection() as conn:
        members = sorted(
            row["entity_id"]
            for row in conn.execute(
                "select entity_id from triage.subset_members where subset_hash = %(h)s",
                {"h": registered[0]["subset_hash"]},
            ).fetchall()
        )
    # Entities 1 and 2 predate the as_of_date; 3 does not.
    assert members == [1, 2]


def test_bias_config_shares_the_same_rendering() -> None:
    """``bias_config.query`` had the identical bare substitution (`bias.py:110`), unreported.

    Its documented examples all quote the placeholder — `docs/fairness.md`, the Chicago 311 and
    DirtyDuck tutorials, `reference/configuration.md` — which is what the bare rendering forced.
    Both spellings work now, the same way they do for a subset.
    """
    from triage.adapters import bias

    assert bias.render_as_of_date is render_as_of_date
    cast = "select entity_id, race from demographics where knowledge_date < {as_of_date}::date"
    quoted = (
        "select entity_id, race from demographics where knowledge_date < '{as_of_date}'"
    )
    assert "'2014-01-01'::date" in render_as_of_date(cast, AS_OF)
    assert "< '2014-01-01'" in render_as_of_date(quoted, AS_OF)
