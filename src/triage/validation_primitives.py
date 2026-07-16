"""Functions for validating input, mostly around database schema and state.

psycopg3-native (ADR-0019): the column-type checks compare against Postgres
``information_schema`` ``data_type`` name strings (what :func:`column_type` returns) rather
than SQLAlchemy DDL type classes. Each ``*_should_*`` helper takes a
``psycopg_pool.DictRowPool``.
"""

from triage.database_reflection import (
    column_type,
    table_exists,
    table_has_column,
    table_has_data,
)

# Postgres ``information_schema.data_type`` names, grouped by the logical category the
# old SQLAlchemy-type checks expressed.
BOOLEANLIKE_TYPES = ("boolean", "smallint", "integer")
TIMELIKE_TYPES = (
    "date",
    "timestamp without time zone",
    "timestamp with time zone",
)
INTLIKE_TYPES = ("bigint", "smallint", "integer")
STRINGLIKE_TYPES = ("character varying", "text", "character")


def table_should_exist(table_name, pool):
    """Ensures that the table exists in the given database

    Args:
        table_name (string) A table name (with schema)
        pool (psycopg_pool.DictRowPool)

    Raises: ValueError if the table does not exist
    """
    if not table_exists(table_name, pool):
        raise ValueError("{} table does not exist".format(table_name))


def table_should_have_column(table_name, column, pool):
    """Ensures that the table has the given column

    Args:
        table_name (string) A table name (with schema)
        column (string) The name of a column
        pool (psycopg_pool.DictRowPool)

    Raises: ValueError if the table does not contain the column
    """
    table_should_exist(table_name, pool)
    if not table_has_column(table_name, column, pool):
        raise ValueError("{} table does not have {} column".format(table_name, column))


def table_should_have_data(table_name, pool):
    """Ensures that the table has at least one row

    Args:
        table_name (string) A table name (with schema)
        pool (psycopg_pool.DictRowPool)

    Raises: ValueError if the table does not have at least one row
    """
    table_should_exist(table_name, pool)
    if not table_has_data(table_name, pool):
        raise ValueError("{} table does not have any data".format(table_name))


def column_should_be_in_types(table_name, column, valid_types, pool):
    """Ensures that the given column is one of the given types

    Args:
        table_name (string) A table name (with schema)
        column (string) The name of a column
        valid_types (list) A list of Postgres ``data_type`` name strings, like ``'boolean'``
        pool (psycopg_pool.DictRowPool)

    Raises: ValueError if the column is not one of the given types
    """
    table_should_have_column(table_name, column, pool)
    reflected_type = column_type(table_name, column, pool)
    if reflected_type not in valid_types:
        raise ValueError(
            "{}.{} should be in types {} but was {}".format(
                table_name, column, valid_types, reflected_type
            )
        )


def column_should_be_booleanlike(table_name, column, pool):
    """Ensures that the given column can be casted to a boolean

    Allows boolean, smallint, and integer, as these are commonly used.
    It does not check that the data in a smallint column all conforms to 0/1

    Args:
        table_name (string) A table name (with schema)
        column (string) The name of a column
        pool (psycopg_pool.DictRowPool)

    Raises: ValueError if the column is not a recognized boolean-compatible type
    """
    table_should_have_column(table_name, column, pool)
    column_should_be_in_types(table_name, column, BOOLEANLIKE_TYPES, pool)


def column_should_be_timelike(table_name, column, pool):
    """Ensures that the given column can be used for temporal data

    Many date/time operations are fairly compatible with each other,
    so this routine is fairly permissive. If you want to be more strict,
    call column_should_be_in_types directly

    Args:
        table_name (string) A table name (with schema)
        column (string) The name of a column
        pool (psycopg_pool.DictRowPool)

    Raises: ValueError if the column is not a recognized temporal type
    """
    table_should_have_column(table_name, column, pool)
    column_should_be_in_types(table_name, column, TIMELIKE_TYPES, pool)


def column_should_be_intlike(table_name, column, pool):
    """Ensures that the given column can act as an integer

    Args:
        table_name (string) A table name (with schema)
        column (string) The name of a column
        pool (psycopg_pool.DictRowPool)

    Raises: ValueError if the column is not a recognized integer type
    """
    table_should_have_column(table_name, column, pool)
    column_should_be_in_types(table_name, column, INTLIKE_TYPES, pool)


def column_should_be_stringlike(table_name, column, pool):
    """Ensures that the given column can act as an string

    Args:
        table_name (string) A table name (with schema)
        column (string) The name of a column
        pool (psycopg_pool.DictRowPool)

    Raises: ValueError if the column is not a recognized string type
    """
    table_should_have_column(table_name, column, pool)
    column_should_be_in_types(table_name, column, STRINGLIKE_TYPES, pool)


def string_is_tablesafe(string):
    if not string:
        return False
    return all((c.isalpha() and c.islower()) or c.isdigit() or c == "_" for c in string)


def table_should_have_entity_date_columns(table_name, pool):
    table_should_have_column(table_name, "entity_id", pool)
    table_should_have_column(table_name, "as_of_date", pool)
