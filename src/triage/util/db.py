# coding: utf-8
import functools
import json

import sqlalchemy
import wrapt

from triage.logging import get_logger

logger = get_logger(__name__)

from datetime import date

from psycopg.rows import dict_row
from psycopg.types.range import DateRange, TimestamptzRange
from psycopg_pool import ConnectionPool
from sqlalchemy import inspect
from sqlalchemy.engine import make_url


def serialize_to_database(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, date):
        return str(obj.isoformat())

    if isinstance(obj, (DateRange, TimestamptzRange)):
        return f"[{obj.lower}, {obj.upper}]"

    return obj


def json_dumps(d):
    return json.dumps(d, default=serialize_to_database)


class SerializableDbEngine(wrapt.ObjectProxy):
    """A sqlalchemy engine that can be serialized across process boundaries.

    Works by saving all kwargs used to create the engine and reconstructs them later.  As a result, the state won't be saved upon serialization/deserialization.
    """

    __slots__ = ("url", "creator", "kwargs")

    def __init__(self, url, *, creator=sqlalchemy.create_engine, **kwargs):
        self.url = make_url(url)
        self.creator = creator
        self.kwargs = kwargs

        engine = creator(url, **kwargs)
        super().__init__(engine)

    def __reduce__(self):
        return (self.__reconstruct__, (self.url, self.creator, self.kwargs))

    def __reduce_ex__(self, protocol):
        # wrapt requires reduce_ex to be implemented
        return self.__reduce__()

    def get_inspector(self):
        return inspect(self.__wrapped__)

    @classmethod
    def __reconstruct__(cls, url, creator, kwargs):
        return cls(url, creator=creator, **kwargs)


create_engine = functools.partial(SerializableDbEngine, json_serializer=json_dumps)


def libpq_conninfo(dburl) -> str:
    """Strip SQLAlchemy's ``+psycopg`` driver tag so a URL is a libpq conninfo string.

    ``cli.resolve_db_url`` and the test fixtures build ``postgresql+psycopg://…`` URLs (the
    form SQLAlchemy/alembic want); psycopg3/libpq wants the bare ``postgresql://…`` form.
    Accepts a string only — pass the password-bearing string from
    ``url.render_as_string(hide_password=False)``, never a bare ``str(URL)`` (which masks
    the password).
    """
    if not isinstance(dburl, str):
        raise TypeError(
            "connection_pool expects a database URL string (password-bearing); got "
            f"{type(dburl).__name__}. Stringify SQLAlchemy URLs with "
            "render_as_string(hide_password=False) first."
        )
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if dburl.startswith(prefix):
            return "postgresql://" + dburl[len(prefix) :]
    return dburl


def connection_pool(
    dburl, *, min_size: int = 1, max_size: int = 10, **kwargs
) -> ConnectionPool:
    """Open a psycopg3 ``ConnectionPool`` for the project database (ADR-0019).

    The single application-side connection factory. Every greenfield adapter takes the
    returned pool and runs raw SQL through ``with pool.connection() as conn`` — which commits
    on clean block exit and rolls back on exception (covering both the old ``engine.connect()``
    read path and the ``engine.begin()`` write-transaction path). Rows come back as dicts
    (``row_factory=dict_row``) so call sites use mapping access. SQLAlchemy is no longer
    involved in application data access; it survives only behind alembic.
    """
    pool = ConnectionPool(
        libpq_conninfo(dburl),
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        open=True,
        **kwargs,
    )
    return pool
