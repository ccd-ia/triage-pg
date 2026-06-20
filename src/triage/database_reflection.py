"""Functions to retrieve basic information about tables in a Postgres database.

psycopg3-native (ADR-0019): each function takes a ``psycopg_pool.ConnectionPool`` and runs
catalog queries directly (``to_regclass`` + ``information_schema``). Table/column identifiers
that must be embedded in SQL go through ``psycopg.sql.Identifier`` (never string-formatted),
and data values are always bound. ``column_type`` returns the Postgres ``data_type`` name
(e.g. ``'character varying'``, ``'integer'``), not a SQLAlchemy type class.
"""

from psycopg import sql


def split_table(table_name):
    """Split a fully-qualified table name into schema and table

    Args:
        table_name (string) A table name, either with or without a schema prefix

    Returns: (tuple) of schema and table name
    """
    table_parts = table_name.split(".")
    if len(table_parts) == 2:
        return tuple(table_parts)
    elif len(table_parts) == 1:
        return (None, table_parts[0])
    else:
        raise ValueError("Table name in unknown format")


def _table_identifier(table_name):
    """A ``psycopg.sql.Identifier`` for a (possibly schema-qualified) table name."""
    schema, table = split_table(table_name)
    if schema:
        return sql.Identifier(schema, table)
    return sql.Identifier(table)


def table_exists(table_name, pool):
    """Checks whether the table exists

    Args:
        table_name (string) A table name (with schema)
        pool (psycopg_pool.ConnectionPool)

    Returns: (boolean) Whether or not the table exists in the database
    """
    with pool.connection() as conn:
        row = conn.execute(
            "select to_regclass(%(qn)s) is not null as exists",
            {"qn": table_name},
        ).fetchone()
    return bool(row["exists"])


def table_has_data(table_name, pool):
    """Check whether the table contains any data

    Args:
        table_name (string) A table name (with schema)
        pool (psycopg_pool.ConnectionPool)

    Returns: (boolean) Whether or not the table has any data
    """
    if not table_exists(table_name, pool):
        return False

    query = sql.SQL("select 1 from {} limit 1").format(_table_identifier(table_name))
    with pool.connection() as conn:
        return conn.execute(query).fetchone() is not None


def table_row_count(table_name, pool):
    """Return the length of the table.

    The table is expected to exist.

    Args:
        table_name (string) A table name (with schema)
        pool (psycopg_pool.ConnectionPool)

    Returns: (int) The number of rows in the table
    """
    query = sql.SQL("select count(*) as n from {}").format(
        _table_identifier(table_name)
    )
    with pool.connection() as conn:
        return conn.execute(query).fetchone()["n"]


def table_has_duplicates(table_name, column_list, pool):
    """Check whether the table has duplicate rows on the set of columns.

    The table is expected to exist and contain the columns in column_list.

    Args:
        table_name (string) A table name (with schema)
        column_list (list) A list of column names
        pool (psycopg_pool.ConnectionPool)

    Returns: (boolean) Whether or not duplicates are found
    """
    if not table_has_data(table_name, pool):
        return False

    cols = sql.SQL(", ").join(sql.Identifier(c) for c in column_list)
    query = sql.SQL(
        "with counts as ("
        "  select {cols}, count(*) as num_records"
        "  from {tbl}"
        "  group by {cols}"
        ") select max(num_records) as max_records from counts"
    ).format(cols=cols, tbl=_table_identifier(table_name))

    with pool.connection() as conn:
        return conn.execute(query).fetchone()["max_records"] > 1


def table_has_column(table_name, column, pool):
    """Check whether the table contains a column of the given name

    The table is expected to exist.

    Args:
        table_name (string) A table name (with schema)
        column (string) A column name
        pool (psycopg_pool.ConnectionPool)

    Returns: (boolean) Whether or not the table contains the column
    """
    schema, table = split_table(table_name)
    with pool.connection() as conn:
        row = conn.execute(
            "select 1 from information_schema.columns"
            " where table_name = %(table)s and column_name = %(column)s"
            " and table_schema = coalesce(%(schema)s, current_schema())"
            " limit 1",
            {"table": table, "column": column, "schema": schema},
        ).fetchone()
    return row is not None


def column_type(table_name, column, pool):
    """Find the Postgres ``data_type`` of the given column in the given table.

    The table is expected to exist, and contain a column of the given name.

    Args:
        table_name (string) A table name (with schema)
        column (string) A column name
        pool (psycopg_pool.ConnectionPool)

    Returns: (str) the ``information_schema`` ``data_type`` name, e.g. ``'character varying'``,
        ``'integer'``, ``'boolean'``, ``'timestamp without time zone'``.
    """
    schema, table = split_table(table_name)
    with pool.connection() as conn:
        row = conn.execute(
            "select data_type from information_schema.columns"
            " where table_name = %(table)s and column_name = %(column)s"
            " and table_schema = coalesce(%(schema)s, current_schema())",
            {"table": table, "column": column, "schema": schema},
        ).fetchone()
    if row is None:
        raise KeyError(f"Column {column} not found")
    return row["data_type"]


def schema_tables(schema_name, pool):
    """The base-table names in the given schema.

    Args:
        schema_name (string) A schema name
        pool (psycopg_pool.ConnectionPool)

    Returns: (list) of table names (str)
    """
    with pool.connection() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables"
            " where table_schema = %(schema)s and table_type = 'BASE TABLE'",
            {"schema": schema_name},
        ).fetchall()
    return [row["table_name"] for row in rows]
