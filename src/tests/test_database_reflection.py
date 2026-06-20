import pytest

from triage.database_reflection import (
    column_type,
    schema_tables,
    split_table,
    table_exists,
    table_has_column,
    table_has_data,
    table_has_duplicates,
    table_row_count,
)


def test_split_table():
    assert split_table("staging.incidents") == ("staging", "incidents")
    assert split_table("incidents") == (None, "incidents")
    with pytest.raises(ValueError):
        split_table("blah.staging.incidents")


def test_table_exists(db_pool):
    with db_pool.connection() as conn:
        conn.execute("create table incidents (col1 varchar)")

    assert table_exists("incidents", db_pool)
    assert not table_exists("compliments", db_pool)


def test_table_has_data(db_pool):
    with db_pool.connection() as conn:
        conn.execute("create table incidents (col1 varchar)")
        conn.execute("create table compliments (col1 varchar)")
        conn.execute("insert into compliments values ('good job')")

    assert table_has_data("compliments", db_pool)
    assert not table_has_data("incidents", db_pool)


def test_table_has_duplicates(db_pool):
    with db_pool.connection() as conn:
        conn.execute("create table events (col1 int, col2 int)")
    assert not table_has_duplicates("events", ["col1"], db_pool)

    with db_pool.connection() as conn:
        conn.execute("insert into events values (1,2)")
    assert not table_has_duplicates("events", ["col1", "col2"], db_pool)

    with db_pool.connection() as conn:
        conn.execute("insert into events values (1,3)")
    assert not table_has_duplicates("events", ["col1", "col2"], db_pool)

    assert table_has_duplicates("events", ["col1"], db_pool)
    assert not table_has_duplicates("events", ["col1", "col2"], db_pool)

    with db_pool.connection() as conn:
        conn.execute("insert into events values (1,2)")
    assert table_has_duplicates("events", ["col1", "col2"], db_pool)


def test_table_row_count(db_pool):
    with db_pool.connection() as conn:
        conn.execute("create table incidents (col1 varchar)")
        conn.execute("insert into incidents values ('a'), ('b'), ('c')")

    assert table_row_count("incidents", db_pool) == 3


def test_table_has_column(db_pool):
    with db_pool.connection() as conn:
        conn.execute("create table incidents (col1 varchar)")

    assert table_has_column("incidents", "col1", db_pool)
    assert not table_has_column("incidents", "col2", db_pool)


def test_column_type(db_pool):
    with db_pool.connection() as conn:
        conn.execute("create table incidents (col1 varchar, col2 int)")

    assert column_type("incidents", "col1", db_pool) == "character varying"
    assert column_type("incidents", "col2", db_pool) == "integer"


def test_schema_tables(db_pool):
    with db_pool.connection() as conn:
        conn.execute("create schema if not exists test")
        conn.execute("create table test.incidents (col1 varchar)")
        conn.execute("create table test.compliments (col1 varchar)")

    tables = schema_tables("test", db_pool)
    assert "incidents" in tables
    assert "compliments" in tables
