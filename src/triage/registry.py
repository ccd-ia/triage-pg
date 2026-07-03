"""Registry control-plane data access (ADR-0002, schema-design.md §3).

The *registry* is the instance-wide control plane — projects, users, membership, and an
append-only submission audit trail — living in its own PostgreSQL database, distinct from the
per-project ``triage`` results schema. This module is the psycopg3 access layer over it, in the
same shape as :mod:`triage.sources`: plain functions over a ``ConnectionPool`` whose connections
use ``dict_row`` (:func:`triage.util.db.connection_pool`), every parameter bound with ``%(name)s``
(never interpolated — SQL-injection + the global hard rule).

It holds **no DB credentials** (ADR-0002/0004): the registry records *which* database a project
routes to (``database_name``), never how to authenticate to it — cloud uses an IAM role per
project, local uses the environment. The write webapp (:mod:`triage.dashboard.write_routes`) is
the HTTP surface over these functions; the auth seam (:mod:`triage.dashboard.auth`) maps a request
principal onto :func:`get_or_create_user`.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from uuid import UUID

from psycopg_pool import ConnectionPool

from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "list_projects",
    "get_project",
    "create_project",
    "mark_project_dropped",
    "get_user_by_email",
    "get_or_create_user",
    "add_member",
    "list_members",
    "member_role",
    "record_submission",
    "list_submissions",
]

# A project slug is url-safe AND doubles as the per-project database name candidate (ADR-0002),
# so it must be a conservative identifier: lowercase letters, digits, hyphen/underscore.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_ROLES = ("owner", "contributor", "viewer")
_PROFILES = ("local", "cloud")


def _validate_slug(slug: str) -> str:
    if not _SLUG.match(slug):
        raise ValueError(
            f"project slug {slug!r} is invalid — expected url-safe lowercase"
            " [a-z0-9][a-z0-9_-]{0,62} (it also names the per-project database, ADR-0002)"
        )
    return slug


# --------------------------------------------------------------------------- projects


def list_projects(
    pool: ConnectionPool, *, include_archived: bool = False
) -> list[dict]:
    """All registry projects (active only unless ``include_archived``), newest first."""
    sql = "select project_id, slug, display_name, database_name, status, created_at, archived_at from registry.projects"
    if not include_archived:
        sql += " where status = 'active'"
    sql += " order by created_at desc"
    with pool.connection() as conn:
        return conn.execute(sql).fetchall()


def get_project(pool: ConnectionPool, slug: str) -> Optional[dict]:
    """The project with this slug, or ``None``."""
    with pool.connection() as conn:
        return conn.execute(
            "select project_id, slug, display_name, database_name, status,"
            " created_at, archived_at from registry.projects where slug = %(slug)s",
            {"slug": slug},
        ).fetchone()


def create_project(
    pool: ConnectionPool,
    *,
    slug: str,
    display_name: str,
    database_name: Optional[str] = None,
) -> dict:
    """Register a project. ``database_name`` defaults to the slug (ADR-0002: the slug names the
    per-project database). Raises ``ValueError`` on a bad slug; the unique constraints surface a
    ``psycopg`` error on a duplicate slug / database_name (fail-fast, not swallowed)."""
    _validate_slug(slug)
    db_name = database_name or slug
    with pool.connection() as conn:
        return conn.execute(
            "insert into registry.projects (slug, display_name, database_name)"
            " values (%(slug)s, %(dn)s, %(db)s)"
            " returning project_id, slug, display_name, database_name, status,"
            " created_at, archived_at",
            {"slug": slug, "dn": display_name, "db": db_name},
        ).fetchone()


def mark_project_dropped(pool: ConnectionPool, *, slug: str) -> dict:
    """Tombstone a project whose database has been dropped (``triage project drop``).

    The row is kept — submissions foreign-key to it and the control plane's history should
    say a project existed — but ``status='dropped'`` takes it out of every active listing
    and the switcher. Raises ``ValueError`` for an unknown slug (fail loud, per the house rule).
    """
    with pool.connection() as conn:
        row = conn.execute(
            "update registry.projects set status = 'dropped', dropped_at = now()"
            " where slug = %(slug)s"
            " returning project_id, slug, display_name, database_name, status,"
            " created_at, archived_at, dropped_at",
            {"slug": slug},
        ).fetchone()
    if row is None:
        raise ValueError(f"no registry project with slug {slug!r}")
    return row


# --------------------------------------------------------------------------- users


def get_user_by_email(pool: ConnectionPool, email: str) -> Optional[dict]:
    with pool.connection() as conn:
        return conn.execute(
            "select user_id, email, display_name, is_admin, created_at from registry.users where email = %(email)s",
            {"email": email},
        ).fetchone()


def get_or_create_user(
    pool: ConnectionPool,
    *,
    email: str,
    display_name: Optional[str] = None,
    is_admin: bool = False,
) -> dict:
    """Idempotently resolve a user by email, creating the row on first sight.

    This is the join point for the auth seam: a request principal (whoever the auth backend says
    is calling) is materialized here so submissions/members can foreign-key to a real user row.
    ``is_admin`` is only applied on INSERT — promotion/demotion is a separate concern, never a
    silent side effect of a login.
    """
    if not email:
        raise ValueError("get_or_create_user requires a non-empty email")
    with pool.connection() as conn:
        # ON CONFLICT DO UPDATE (not DO NOTHING) so RETURNING always yields the row.
        return conn.execute(
            "insert into registry.users (email, display_name, is_admin)"
            " values (%(email)s, %(dn)s, %(admin)s)"
            " on conflict (email) do update set"
            "   display_name = coalesce(excluded.display_name, registry.users.display_name)"
            " returning user_id, email, display_name, is_admin, created_at",
            {"email": email, "dn": display_name, "admin": is_admin},
        ).fetchone()


# --------------------------------------------------------------------------- membership


def add_member(
    pool: ConnectionPool,
    *,
    project_id: UUID,
    user_id: UUID,
    role: str = "contributor",
) -> dict:
    """Add (or re-role) a user on a project. Idempotent on ``(project_id, user_id)``."""
    if role not in _ROLES:
        raise ValueError(f"role must be one of {_ROLES}; got {role!r}")
    with pool.connection() as conn:
        return conn.execute(
            "insert into registry.project_members (project_id, user_id, role)"
            " values (%(p)s, %(u)s, %(r)s)"
            " on conflict (project_id, user_id) do update set role = excluded.role"
            " returning project_id, user_id, role, added_at",
            {"p": project_id, "u": user_id, "r": role},
        ).fetchone()


def list_members(pool: ConnectionPool, *, project_id: UUID) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            "select m.project_id, m.user_id, m.role, m.added_at,"
            " u.email, u.display_name"
            " from registry.project_members m"
            " join registry.users u using (user_id)"
            " where m.project_id = %(p)s order by m.added_at",
            {"p": project_id},
        ).fetchall()


def member_role(
    pool: ConnectionPool, *, project_id: UUID, user_id: UUID
) -> Optional[str]:
    """The user's role on the project, or ``None`` if they are not a member."""
    with pool.connection() as conn:
        row = conn.execute(
            "select role from registry.project_members where project_id = %(p)s and user_id = %(u)s",
            {"p": project_id, "u": user_id},
        ).fetchone()
    return row["role"] if row else None


# --------------------------------------------------------------------------- submissions


def record_submission(
    pool: ConnectionPool,
    *,
    project_id: UUID,
    submitted_by: Optional[UUID],
    experiment_hash: Optional[str] = None,
    profile: str = "local",
    batch_job_id: Optional[str] = None,
) -> dict:
    """Append one row to the submission audit trail (who submitted what, where it routed).

    Append-only by design (schema-design §3): a submission is never mutated. ``experiment_hash``
    maps to ``triage.experiments`` in the project DB once the run has planned; ``batch_job_id`` is
    the AWS Batch id under the cloud profile (``None`` locally)."""
    if profile not in _PROFILES:
        raise ValueError(f"profile must be one of {_PROFILES}; got {profile!r}")
    with pool.connection() as conn:
        return conn.execute(
            "insert into registry.submissions"
            " (project_id, submitted_by, experiment_hash, profile, batch_job_id)"
            " values (%(p)s, %(u)s, %(h)s, %(prof)s, %(job)s)"
            " returning submission_id, project_id, submitted_by, experiment_hash,"
            " profile, batch_job_id, submitted_at",
            {
                "p": project_id,
                "u": submitted_by,
                "h": experiment_hash,
                "prof": profile,
                "job": batch_job_id,
            },
        ).fetchone()


def list_submissions(
    pool: ConnectionPool,
    *,
    project_id: Optional[UUID] = None,
    limit: int = 100,
) -> list[dict]:
    """The submission audit trail, newest first, optionally scoped to one project."""
    params: dict[str, Any] = {"limit": limit}
    sql = (
        "select s.submission_id, s.project_id, p.slug as project_slug,"
        " s.submitted_by, u.email as submitted_by_email, s.experiment_hash,"
        " s.profile, s.batch_job_id, s.submitted_at"
        " from registry.submissions s"
        " join registry.projects p using (project_id)"
        " left join registry.users u on u.user_id = s.submitted_by"
    )
    if project_id is not None:
        sql += " where s.project_id = %(p)s"
        params["p"] = project_id
    sql += " order by s.submitted_at desc limit %(limit)s"
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()
