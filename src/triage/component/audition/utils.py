import pandas as pd


def make_list(a):
    return [a] if not isinstance(a, list) else a


def str_in_sql(values):
    return ",".join(map(lambda x: "'{}'".format(x), values))


def read_sql_pool(db_engine, query, params=None):
    """Run ``query`` against a psycopg3 pool and return a ``pandas.DataFrame``.

    ``pandas.read_sql`` does not accept a ``psycopg_pool.ConnectionPool`` and
    misreads a raw psycopg3 ``dict_row`` cursor (it pairs dict rows against the
    cursor description and yields the column names as the data). So we borrow a
    connection, execute with ``%(name)s`` binds, and build the frame from the
    fetched dict rows directly — column names come out correct, and an empty
    result still produces the right columns from the cursor description.

    Args:
        db_engine (psycopg_pool.ConnectionPool): the project connection pool
        query (str): SQL with ``%(name)s`` placeholders (or none)
        params (dict | None): bind values for the placeholders
    """
    with db_engine.connection() as conn:
        cur = conn.execute(query, params)
        rows = cur.fetchall()
        if not rows:
            columns = [desc.name for desc in cur.description] if cur.description else []
            return pd.DataFrame(columns=columns)
        return _coerce_decimal_columns(pd.DataFrame(rows))


def _coerce_decimal_columns(df):
    """Coerce ``Decimal``-valued object columns to float in-place.

    psycopg3 maps PostgreSQL ``numeric`` (e.g. ``avg()`` / ``generate_series()``
    output) to Python ``decimal.Decimal``, which lands in pandas as an ``object``
    column — and ``DataFrame.plot`` then rejects it as "no numeric data". The old
    SQLAlchemy ``read_sql`` returned these as float, so reproduce that: for each
    object column, attempt a numeric cast and keep it only when every non-null
    value converts (leaving genuine text / date columns untouched).
    """
    for col in df.columns:
        if df[col].dtype != object:
            continue
        original_notna = df[col].notna()
        if not original_notna.any():
            continue
        converted = pd.to_numeric(df[col], errors="coerce")
        if converted[original_notna].notna().all():
            df[col] = converted
    return df
