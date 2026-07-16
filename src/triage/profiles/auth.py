"""Auth adapters — the password / RDS-IAM connection seam (ADR-0004, cloud-profile-spec §2).

``auth`` produces the project ``ConnectionPool``. The local adapter is today's static-password
:func:`triage.util.db.connection_pool` verbatim. The cloud adapter injects a fresh short-lived
RDS IAM token **per physical connection** via a custom psycopg ``connection_class`` — relying on
the fact that a token only has to be valid *at connect time*; once a connection authenticates the
session persists regardless of token expiry, so no refresh timer is needed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from triage.logging import get_logger
from triage.util.db import DictRowPool, connection_pool

logger = get_logger(__name__)

__all__ = ["LocalAuth", "CloudAuth", "make_iam_connection_class"]

# The Amazon RDS CA bundle path. AMIs/containers bake this in; the env var lets a deployment
# override it. ``verify-full`` requires it so the client validates the RDS server certificate.
_DEFAULT_RDS_CA_BUNDLE = "/etc/ssl/certs/rds-combined-ca-bundle.pem"

# Recycle pooled connections within ~the token TTL (15 min) so none drifts far past expiry.
# Defensive only — correctness does not depend on it (a live session survives token expiry).
_TOKEN_TTL_SECONDS = 600


class LocalAuth:
    """Static-password auth: today's :func:`triage.util.db.connection_pool` (no behavior change).

    The conninfo is resolved by :func:`triage.cli.resolve_db_url` (``DATABASE_URL`` / ``PG*`` /
    ``database.yaml``) and handed in at construction.
    """

    def __init__(self, dburl: str) -> None:
        self._dburl = dburl

    def open_pool(self, *, min_size: int = 1, max_size: int = 10) -> DictRowPool:
        return connection_pool(self._dburl, min_size=min_size, max_size=max_size)


def make_iam_connection_class(
    token_provider: Callable[[], str],
    *,
    sslrootcert: str = _DEFAULT_RDS_CA_BUNDLE,
) -> type[Connection]:
    """Build a psycopg ``Connection`` subclass that fetches a fresh IAM token per connect.

    ``token_provider`` is the injectable seam (cloud-profile-spec §6): the real one calls
    boto3 ``rds.generate_db_auth_token``; tests pass a stub returning a fixed string. Every
    physical connect calls it, stamps the token as the ``password``, and forces
    ``sslmode=verify-full`` + the RDS CA bundle (IAM auth requires TLS).
    """

    class _IamConnection(Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
            kwargs["password"] = token_provider()
            kwargs.setdefault("sslmode", "verify-full")
            kwargs.setdefault("sslrootcert", sslrootcert)
            return super().connect(conninfo, **kwargs)

    return _IamConnection


class CloudAuth:
    """RDS IAM auth: a pool whose every physical connection carries a fresh IAM token (ADR-0004).

    The conninfo (host/port/db/user, **no password**) is fixed at construction; the
    ``connection_class`` injects the token per connect. ``token_provider`` defaults to the real
    boto3 generator but is injectable so the suite never touches live AWS (cloud-profile-spec §6).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        dbname: str,
        user: str,
        region: str,
        token_provider: Callable[[], str] | None = None,
        sslrootcert: str = _DEFAULT_RDS_CA_BUNDLE,
    ) -> None:
        self._host = host
        self._port = port
        self._dbname = dbname
        self._user = user
        self._region = region
        self._sslrootcert = sslrootcert
        self._token_provider = token_provider or self._default_token_provider

    def _default_token_provider(self) -> str:
        """The real RDS IAM token: boto3 ``rds.generate_db_auth_token`` (cloud-profile-spec §2.2)."""
        import boto3

        client = boto3.client("rds", region_name=self._region)
        return client.generate_db_auth_token(
            DBHostname=self._host,
            Port=self._port,
            DBUsername=self._user,
            Region=self._region,
        )

    def _base_conninfo(self) -> str:
        """The password-less libpq conninfo; the token enters per-connect via the class."""
        return (
            f"host={self._host} port={self._port}"
            + f" dbname={self._dbname} user={self._user}"
        )

    def open_pool(self, *, min_size: int = 1, max_size: int = 10) -> DictRowPool:
        connection_class = make_iam_connection_class(
            self._token_provider, sslrootcert=self._sslrootcert
        )
        # The IAM subclass still yields dict rows (row_factory below), so the pool IS a
        # DictRowPool — the cast records what the runtime kwargs guarantee.
        return cast(
            DictRowPool,
            ConnectionPool(
                self._base_conninfo(),
                connection_class=connection_class,
                kwargs={"row_factory": dict_row},
                max_lifetime=_TOKEN_TTL_SECONDS,
                min_size=min_size,
                max_size=max_size,
                open=True,
            ),
        )
