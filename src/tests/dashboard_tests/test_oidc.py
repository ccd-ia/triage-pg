"""OIDC auth backend tests (ADR-0028) — full flow against a stubbed IdP, no network.

The IdP round-trips are module-level seams (`fetch_discovery` / `exchange_code` /
`decode_id_token`); these tests stub them and drive the REAL endpoints through TestClient:
login carries signed state+nonce, the callback exchanges the code and sets the session
cookie, `authenticate` resolves it to a Principal, tampering and state mismatch fail loud,
and the API's unauthenticated shape carries `login_url` for the SPA. TrustedHeaderAuth
behavior is untouched (the seam holds) — covered by the existing write-API tests.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from triage.component.registry_schema import upgrade_registry_db
from triage.dashboard import oidc as oidc_module
from triage.dashboard.app import create_app
from triage.dashboard.auth import resolve_auth_backend
from triage.dashboard.oidc import OidcAuth, OidcSettings

ISSUER = "https://idp.test"
DISCOVERY = {
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
}
SETTINGS = OidcSettings(
    issuer=ISSUER,
    client_id="triage-dashboard",
    client_secret="s3cret",
    session_secret="session-signing-secret",
)


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("TRIAGE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("TRIAGE_OIDC_CLIENT_ID", SETTINGS.client_id)
    monkeypatch.setenv("TRIAGE_OIDC_CLIENT_SECRET", SETTINGS.client_secret)
    monkeypatch.setenv("TRIAGE_SESSION_SECRET", SETTINGS.session_secret)
    monkeypatch.setenv("TRIAGE_ADMIN_EMAILS", "root@test")


@pytest.fixture
def stub_idp(monkeypatch):
    """Stub the three IdP round-trips; record what the callback exchanged."""
    calls: dict[str, Any] = {}

    def fake_discovery(issuer):
        assert issuer == ISSUER
        return DISCOVERY

    def fake_exchange(token_endpoint, code, redirect_uri, settings):
        calls["exchange"] = {"code": code, "redirect_uri": redirect_uri}
        assert token_endpoint == DISCOVERY["token_endpoint"]
        return {"id_token": "stub-id-token"}

    def fake_decode(id_token, jwks_uri, settings, nonce):
        calls["decode"] = {"id_token": id_token, "nonce": nonce}
        return {"email": "ada@test", "name": "Ada"}

    monkeypatch.setattr(oidc_module, "fetch_discovery", fake_discovery)
    monkeypatch.setattr(oidc_module, "exchange_code", fake_exchange)
    monkeypatch.setattr(oidc_module, "decode_id_token", fake_decode)
    return calls


@pytest.fixture
def client(db_url, db_pool_greenfield, oidc_env):
    upgrade_registry_db(db_url)
    app = create_app(
        pool=db_pool_greenfield,
        registry_pool=db_pool_greenfield,
        auth_backend=OidcAuth(),
    )
    with TestClient(app, follow_redirects=False) as c:
        yield c


def test_settings_fail_fast_naming_the_variable(monkeypatch, oidc_env):
    monkeypatch.delenv("TRIAGE_SESSION_SECRET")
    with pytest.raises(ValueError, match="TRIAGE_SESSION_SECRET"):
        OidcSettings.from_env()


def test_resolve_backend_oidc(oidc_env):
    backend = resolve_auth_backend("oidc")
    assert isinstance(backend, OidcAuth) and backend.mode == "oidc"


def test_unauthenticated_api_request_carries_login_url(client):
    r = client.get("/api/me")
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert detail["login_url"] == "/auth/login"


def test_login_redirects_to_idp_with_state_and_nonce(client, stub_idp):
    r = client.get("/auth/login")
    assert r.status_code == 302
    target = urlparse(r.headers["location"])
    assert r.headers["location"].startswith(DISCOVERY["authorization_endpoint"])
    q = parse_qs(target.query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == [SETTINGS.client_id]
    assert q["state"][0] and q["nonce"][0]
    assert "triage_oidc_state" in r.cookies


def test_full_login_flow_establishes_session(client, stub_idp):
    # 1) login → capture the state the IdP would echo back
    login = client.get("/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

    # 2) the IdP redirects back with code+state; the callback exchanges + sets the session
    cb = client.get(f"/auth/callback?code=authz-code&state={state}")
    assert cb.status_code == 302 and cb.headers["location"] == "/"
    assert stub_idp["exchange"]["code"] == "authz-code"
    assert oidc_module.SESSION_COOKIE in client.cookies

    # 3) the session resolves to a Principal on the API (auth_mode tells the SPA it's oidc)
    me = client.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "ada@test"
    assert body["is_admin"] is False  # no default admin under oidc
    assert body["auth_mode"] == "oidc"

    # 4) logout clears the session → back to 401-with-login_url
    out = client.get("/auth/logout")
    assert out.status_code == 302
    assert client.get("/api/me").status_code == 401


def test_state_mismatch_is_rejected(client, stub_idp):
    client.get("/auth/login")
    r = client.get("/auth/callback?code=x&state=forged-state")
    assert r.status_code == 400
    assert "state" in r.json()["detail"]


def test_tampered_session_cookie_is_401(client, stub_idp):
    login = client.get("/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    client.get(f"/auth/callback?code=x&state={state}")
    client.cookies.set(oidc_module.SESSION_COOKIE, "tampered.payload.signature")
    r = client.get("/api/me")
    assert r.status_code == 401
    assert r.json()["detail"]["login_url"] == "/auth/login"


def test_admin_comes_from_env_not_the_idp(client, stub_idp, monkeypatch):
    # ada@test is not in TRIAGE_ADMIN_EMAILS → admin-only routes 403 even with a session
    login = client.get("/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    client.get(f"/auth/callback?code=x&state={state}")
    r = client.post("/api/projects", json={"slug": "nope", "display_name": "Nope"})
    assert r.status_code == 403


def test_auth_endpoints_404_under_trusted_mode(db_url, db_pool_greenfield):
    upgrade_registry_db(db_url)
    app = create_app(pool=db_pool_greenfield, registry_pool=db_pool_greenfield)
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/auth/login").status_code == 404
        assert c.get("/auth/logout").status_code == 404
