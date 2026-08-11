"""``resolve_db_url`` precedence, and saying so when the environment loses.

A ``database.yaml`` in the current directory outranks ``PG*``/``DATABASE_URL`` by
design (the documented precedence in ``resolve_db_url``). The cost is that the
repo root ships example config files, so a command run from there silently
targets a tutorial database even when the caller's environment points somewhere
else entirely — noticed 2026-08-11 while standing triage-pg up against a host
project. The precedence stays; it just stops being silent.
"""

from __future__ import annotations

import pytest
from loguru import logger as loguru_logger

import triage.cli as cli

DATABASE_YAML = """
host: yaml-host
port: 5432
db: tutorial_db
user: tutorial_user
pass: tutorial_pass
"""


@pytest.fixture(name="warnings")
def fixture_warnings():
    """Collect WARNING-and-above loguru messages (the repo logs through loguru)."""
    messages: list[str] = []
    sink_id = loguru_logger.add(messages.append, level="WARNING", format="{message}")
    yield messages
    loguru_logger.remove(sink_id)


@pytest.fixture(name="cwd_with_yaml")
def fixture_cwd_with_yaml(tmp_path, monkeypatch):
    (tmp_path / "database.yaml").write_text(DATABASE_YAML)
    monkeypatch.chdir(tmp_path)
    for key in (
        "DATABASE_URL",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_warns_when_database_yaml_shadows_a_different_env(
    cwd_with_yaml, monkeypatch, warnings
):
    monkeypatch.setenv("PGHOST", "prod-host")
    monkeypatch.setenv("PGDATABASE", "prod_db")

    url = cli.resolve_db_url(None)

    assert "yaml-host" in url and "tutorial_db" in url  # precedence unchanged
    assert len(warnings) == 1
    message = warnings[0]
    assert "database.yaml" in message
    assert "tutorial_db" in message and "prod_db" in message
    assert "--dbfile" in message


def test_quiet_when_the_env_names_the_same_database(
    cwd_with_yaml, monkeypatch, warnings
):
    monkeypatch.setenv("PGHOST", "yaml-host")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGDATABASE", "tutorial_db")

    cli.resolve_db_url(None)

    assert warnings == []


def test_quiet_when_the_environment_is_empty(cwd_with_yaml, warnings):
    cli.resolve_db_url(None)

    assert warnings == []


def test_explicit_dbfile_is_not_second_guessed(cwd_with_yaml, monkeypatch, warnings):
    """``--dbfile`` is the caller naming the database; that is not a surprise."""
    monkeypatch.setenv("PGHOST", "prod-host")
    monkeypatch.setenv("PGDATABASE", "prod_db")

    url = cli.resolve_db_url(cwd_with_yaml / "database.yaml")

    assert "tutorial_db" in url
    assert warnings == []


def test_database_url_env_target_is_compared_not_just_detected(
    cwd_with_yaml, monkeypatch, warnings
):
    """A ``DATABASE_URL`` pointing at the same place as the file warrants no warning."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://tutorial_user@yaml-host:5432/tutorial_db"
    )
    cli.resolve_db_url(None)
    assert warnings == []

    monkeypatch.setenv("DATABASE_URL", "postgresql://someone@prod-host:5432/prod_db")
    cli.resolve_db_url(None)
    assert len(warnings) == 1
    assert "prod_db" in warnings[0]
