"""OIDC auth backend — real IdP-backed identity behind the ADR-0024 seam (ADR-0028).

``TRIAGE_AUTH=oidc`` selects :class:`OidcAuth`: the standard Authorization Code flow against
any discovery-speaking IdP (Keycloak / Auth0 / Google / Entra). ``/auth/login`` redirects to
the IdP with a signed state+nonce cookie; ``/auth/callback`` exchanges the code, validates the
``id_token`` (issuer / audience / expiry / nonce, against the IdP's JWKS), and sets a signed
HttpOnly session cookie; ``authenticate`` resolves that cookie to a
:class:`~triage.dashboard.auth.Principal` — materialized in ``registry.users`` exactly as
TrustedHeaderAuth does. API-vs-browser stays distinct: an unauthenticated **API** request gets
a 401 whose JSON detail carries ``login_url`` (the SPA redirects itself); only the browser
endpoints 302.

Configuration is env-only and fail-fast (the credential hard rule): ``TRIAGE_OIDC_ISSUER``,
``TRIAGE_OIDC_CLIENT_ID``, ``TRIAGE_OIDC_CLIENT_SECRET``, ``TRIAGE_SESSION_SECRET``
(+ optional ``TRIAGE_SESSION_MAX_AGE``, seconds, default 8h). Admin mapping stays
``TRIAGE_ADMIN_EMAILS`` — with **no default admin** (unlike the laptop TrustedHeaderAuth):
a shared deployment must name its admins explicitly.

The IdP round-trips (:func:`fetch_discovery`, :func:`exchange_code`, :func:`decode_id_token`)
are module-level seams so the test suite stubs them — no live IdP anywhere in CI. The heavy
dependencies (authlib, itsdangerous, httpx) import lazily inside functions, so this module is
importable (for the router) without the ``triage[oidc]`` extra installed.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Any, NoReturn, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from triage.util.db import DictRowPool

from triage import registry
from triage.dashboard.auth import Principal
from triage.logging import get_logger

logger = get_logger(__name__)

__all__ = ["OidcAuth", "OidcSettings", "auth_router", "SESSION_COOKIE", "LOGIN_PATH"]

SESSION_COOKIE = "triage_session"
_STATE_COOKIE = "triage_oidc_state"
LOGIN_PATH = "/auth/login"

_REQUIRED_ENV = (
    "TRIAGE_OIDC_ISSUER",
    "TRIAGE_OIDC_CLIENT_ID",
    "TRIAGE_OIDC_CLIENT_SECRET",
    "TRIAGE_SESSION_SECRET",
)


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    client_id: str
    client_secret: str
    session_secret: str
    session_max_age: int = 28800  # 8h

    @classmethod
    def from_env(cls) -> "OidcSettings":
        missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
        if missing:
            raise ValueError(
                f"TRIAGE_AUTH=oidc requires {', '.join(missing)} — set them in the"
                " environment (direnv/.envrc; never in code or config files, per the"
                " credential hard rule)."
            )
        return cls(
            issuer=os.environ["TRIAGE_OIDC_ISSUER"].rstrip("/"),
            client_id=os.environ["TRIAGE_OIDC_CLIENT_ID"],
            client_secret=os.environ["TRIAGE_OIDC_CLIENT_SECRET"],
            session_secret=os.environ["TRIAGE_SESSION_SECRET"],
            session_max_age=int(os.environ.get("TRIAGE_SESSION_MAX_AGE", "28800")),
        )


def _serializer(secret: str):
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(secret, salt="triage-oidc")


def _unauthenticated(message: str = "authentication required") -> NoReturn:
    # A dict detail so the SPA can follow login_url; ApiError-side handling in api/client.ts.
    raise HTTPException(
        status_code=401, detail={"message": message, "login_url": LOGIN_PATH}
    )


class OidcAuth:
    """The ADR-0024 :class:`AuthBackend` resolved from the OIDC session cookie."""

    mode = "oidc"

    def __init__(self, settings: Optional[OidcSettings] = None) -> None:
        self.settings = settings or OidcSettings.from_env()
        raw = os.environ.get("TRIAGE_ADMIN_EMAILS", "")
        # Deliberately NO default admin (contrast TrustedHeaderAuth's laptop convenience):
        # under real auth on a shared deployment, admin must be an explicit configuration act.
        self._admin_emails = frozenset(e.strip() for e in raw.split(",") if e.strip())

    def authenticate(self, request: Request, registry_pool: DictRowPool) -> Principal:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            _unauthenticated()
        from itsdangerous import BadSignature, SignatureExpired

        try:
            data: dict[str, Any] = _serializer(self.settings.session_secret).loads(
                raw, max_age=self.settings.session_max_age
            )
        except SignatureExpired:
            _unauthenticated("session expired — sign in again")
        except BadSignature:
            _unauthenticated("session invalid — sign in again")
        email = (data.get("email") or "").strip()
        if not email:
            _unauthenticated("session carries no identity — sign in again")
        row = registry.get_or_create_user(
            registry_pool,
            email=email,
            display_name=data.get("name"),
            is_admin=email in self._admin_emails,
        )
        return Principal(
            user_id=row["user_id"],
            email=row["email"],
            display_name=row["display_name"],
            is_admin=email in self._admin_emails,
        )


# --------------------------------------------------------------------- IdP round-trips
# Module-level seams: the browser endpoints call these through the module namespace so the
# test suite can stub them (no live IdP in CI). Real implementations use httpx + authlib.


def fetch_discovery(issuer: str) -> dict[str, Any]:
    """GET the OIDC discovery document (authorization/token/jwks endpoints)."""
    import httpx

    response = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10)
    response.raise_for_status()
    return response.json()


def exchange_code(
    token_endpoint: str, code: str, redirect_uri: str, settings: OidcSettings
) -> dict[str, Any]:
    """Exchange the authorization code for tokens at the IdP's token endpoint."""
    import httpx

    response = httpx.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(settings.client_id, settings.client_secret),
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def decode_id_token(
    id_token: str, jwks_uri: str, settings: OidcSettings, nonce: str
) -> dict[str, Any]:
    """Validate the id_token (signature via JWKS; iss/aud/exp/nonce) and return its claims."""
    import httpx
    from authlib.jose import jwt

    keys = httpx.get(jwks_uri, timeout=10).json()
    claims = jwt.decode(
        id_token,
        keys,
        claims_options={
            "iss": {"essential": True, "value": settings.issuer},
            "aud": {"essential": True, "value": settings.client_id},
            "nonce": {"essential": True, "value": nonce},
        },
    )
    claims.validate()
    return dict(claims)


# --------------------------------------------------------------------- browser endpoints

auth_router = APIRouter()


def _oidc_backend(request: Request) -> OidcAuth:
    backend = getattr(request.app.state, "auth_backend", None)
    if not isinstance(backend, OidcAuth):
        raise HTTPException(
            status_code=404, detail="OIDC auth is not enabled (TRIAGE_AUTH)"
        )
    return backend


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


@auth_router.get("/auth/login")
def login(request: Request) -> RedirectResponse:
    """Kick off the Authorization Code flow: signed state+nonce cookie → IdP redirect."""
    backend = _oidc_backend(request)
    settings = backend.settings
    discovery = fetch_discovery(settings.issuer)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    redirect_uri = str(request.base_url).rstrip("/") + "/auth/callback"
    url = (
        discovery["authorization_endpoint"]
        + "?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": settings.client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
            }
        )
    )
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        _STATE_COOKIE,
        _serializer(settings.session_secret).dumps(
            {"state": state, "nonce": nonce, "redirect_uri": redirect_uri}
        ),
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )
    return response


@auth_router.get("/auth/callback")
def callback(request: Request, code: str, state: str) -> RedirectResponse:
    """Complete the flow: verify state, exchange the code, validate id_token, set the session."""
    backend = _oidc_backend(request)
    settings = backend.settings
    raw = request.cookies.get(_STATE_COOKIE)
    if not raw:
        raise HTTPException(
            status_code=400, detail="missing OIDC state cookie — restart login"
        )
    from itsdangerous import BadSignature, SignatureExpired

    try:
        saved = _serializer(settings.session_secret).loads(raw, max_age=600)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(
            status_code=400,
            detail="OIDC state cookie invalid or expired — restart login",
        ) from exc
    if saved.get("state") != state:
        raise HTTPException(
            status_code=400, detail="OIDC state mismatch — restart login"
        )

    discovery = fetch_discovery(settings.issuer)
    tokens = exchange_code(
        discovery["token_endpoint"], code, saved["redirect_uri"], settings
    )
    claims = decode_id_token(
        tokens["id_token"], discovery["jwks_uri"], settings, saved["nonce"]
    )
    email = (claims.get("email") or "").strip()
    if not email:
        raise HTTPException(
            status_code=400,
            detail="id_token carries no email claim — grant the 'email' scope to this client",
        )
    logger.info("oidc: session established for %s", email)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        _serializer(settings.session_secret).dumps(
            {"email": email, "name": claims.get("name")}
        ),
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )
    response.delete_cookie(_STATE_COOKIE)
    return response


@auth_router.get("/auth/logout")
def logout(request: Request) -> RedirectResponse:
    _oidc_backend(request)
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response
