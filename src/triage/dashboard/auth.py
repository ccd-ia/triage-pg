"""The write-webapp user-auth seam (ADR-0002 registry auth; ADR-0024).

This is the identity boundary for the *write* surface: who is making a request, and are they an
admin. It is deliberately **separate** from the profile's DB-auth adapter
(:class:`triage.profiles.AuthAdapter`, which produces a *database* connection pool via password or
RDS-IAM) — that is machine↔database auth; this is human↔webapp auth.

The seam is one :class:`AuthBackend` protocol with one dependency (:func:`current_principal`). v1
ships :class:`TrustedHeaderAuth` — a **local / single-tenant** backend that trusts an
``X-Triage-User`` request header (or a configured dev default) and materializes that identity in
``registry.users``. It is NOT real authentication: it trusts whatever the caller (or a trusted
reverse proxy in front of it) asserts. Real auth (OIDC / session cookies / an SSO-mapped identity)
is a drop-in replacement for this one class — the routes only ever see a resolved
:class:`Principal`, so nothing downstream changes. Selection is by the ``TRIAGE_AUTH`` env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from psycopg_pool import ConnectionPool

from triage import registry
from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "Principal",
    "AuthBackend",
    "TrustedHeaderAuth",
    "resolve_auth_backend",
    "current_principal",
    "require_admin",
]

_USER_HEADER = "X-Triage-User"


@dataclass(frozen=True)
class Principal:
    """The resolved caller identity handed to write routes (never a raw header)."""

    user_id: UUID
    email: str
    display_name: Optional[str]
    is_admin: bool


@runtime_checkable
class AuthBackend(Protocol):
    """Resolve a request + the registry pool into a :class:`Principal`, or raise 401."""

    def authenticate(self, request: Request, registry_pool: ConnectionPool) -> Principal: ...


class TrustedHeaderAuth:
    """Local / single-tenant backend: trust the ``X-Triage-User`` email header.

    Resolution order for the caller's email: the ``X-Triage-User`` header, else the
    ``TRIAGE_DEV_USER`` env default (so a laptop dev session Just Works without setting a header).
    The email is upserted into ``registry.users`` (:func:`registry.get_or_create_user`) so
    submissions/members can foreign-key to a real row. Admin is granted when the email is listed in
    ``TRIAGE_ADMIN_EMAILS`` (comma-separated).

    Security posture: this backend TRUSTS the header — deploy it only where the caller is trusted
    (a laptop, or behind a reverse proxy that sets the header from a real session). It exists so the
    write surface is usable in the local profile without wiring an IdP; swap in a real backend for
    any shared/public deployment.
    """

    def __init__(
        self,
        *,
        default_user: Optional[str] = None,
        admin_emails: Optional[frozenset[str]] = None,
    ) -> None:
        self._default_user = default_user or os.environ.get("TRIAGE_DEV_USER") or "dev@localhost"
        if admin_emails is None:
            raw = os.environ.get("TRIAGE_ADMIN_EMAILS", "")
            admin_emails = frozenset(e.strip() for e in raw.split(",") if e.strip())
        # A single-tenant laptop dev is an admin of their own instance by default: if no admin
        # list is configured, the default dev user is the admin (so project creation works out of
        # the box). Configure TRIAGE_ADMIN_EMAILS to lock this down.
        self._admin_emails = admin_emails or frozenset({self._default_user})

    def authenticate(self, request: Request, registry_pool: ConnectionPool) -> Principal:
        email = (request.headers.get(_USER_HEADER) or self._default_user).strip()
        if not email:
            raise HTTPException(status_code=401, detail="no caller identity")
        row = registry.get_or_create_user(registry_pool, email=email, is_admin=email in self._admin_emails)
        # is_admin is authoritative from config (the header can't self-promote): a user created
        # earlier as non-admin still gets admin here if now listed, and vice-versa.
        return Principal(
            user_id=row["user_id"],
            email=row["email"],
            display_name=row["display_name"],
            is_admin=email in self._admin_emails,
        )


def resolve_auth_backend(name: Optional[str] = None) -> AuthBackend:
    """Build the auth backend named by ``name`` (default: ``TRIAGE_AUTH`` env, else ``trusted``).

    Only ``trusted`` ships in v1. An unknown name fails loudly rather than silently falling back —
    a misconfigured auth mode must not degrade to "trust everything" by accident.
    """
    name = (name or os.environ.get("TRIAGE_AUTH") or "trusted").strip().lower()
    if name == "trusted":
        return TrustedHeaderAuth()
    raise ValueError(
        f"unknown TRIAGE_AUTH backend {name!r} — v1 supports 'trusted'"
        " (real IdP-backed auth is the documented drop-in extension, ADR-0024)"
    )


def _registry_pool(request: Request) -> ConnectionPool:
    """The registry control-plane pool, or 503 when the app has no registry configured.

    The read dashboard runs without a registry (single-project, read-only); the write surface
    requires one. Kept local (not imported from app) to avoid an import cycle, mirroring
    ``routes._pool``.
    """
    pool = getattr(request.app.state, "registry_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "registry not configured — the write webapp needs a registry control-plane DB."
                " Set TRIAGE_REGISTRY_URL (or pass registry_pool to create_app) and run"
                " `just alembic-registry upgrade head` against it (ADR-0002)."
            ),
        )
    return pool


def current_principal(request: Request) -> Principal:
    """FastAPI dependency: the resolved caller identity for a write request."""
    backend: AuthBackend = request.app.state.auth_backend
    return backend.authenticate(request, _registry_pool(request))


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    """FastAPI dependency: like :func:`current_principal` but 403s non-admins."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin privileges required")
    return principal
