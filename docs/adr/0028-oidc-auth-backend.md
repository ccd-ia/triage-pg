# 0028. Real webapp auth: in-app OIDC Authorization Code flow

- Status: Accepted
- Date: 2026-07-03
- Deciders: Adolfo (scope), Claude (recommendation per the v1-completion plan)
- Status update (2026-07-03): Implemented — `triage/dashboard/oidc.py` (`OidcAuth` behind the
  ADR-0024 `AuthBackend` seam + `/auth/login|callback|logout`), selected by `TRIAGE_AUTH=oidc`,
  optional extra `triage[oidc]` (authlib + itsdangerous + httpx); stub-IdP tests in
  `dashboard_tests/test_oidc.py`; the SPA follows 401 `login_url` and shows logout under OIDC.

ADR-0024 shipped `TrustedHeaderAuth` (laptop-only, trusts a header) and promised real
IdP-backed auth as a drop-in for that one class. This ADR records the flow choice.
(Three-criteria check: *hard to reverse* — the session model and IdP coupling shape every
shared deployment; *surprising without context* — an app that already works "with auth"
growing a second auth mechanism needs its rationale recorded; *real trade-off* — in-app flow
vs proxy-injected identity are both credible.)

## Decision

**In-app OIDC Authorization Code flow**, IdP-agnostic via discovery
(`/.well-known/openid-configuration` — Keycloak, Auth0, Google, Entra all speak it):
`/auth/login` redirects to the IdP with a signed state+nonce cookie; `/auth/callback`
exchanges the code, validates the `id_token` (issuer, audience, expiry, nonce — authlib JOSE
against the IdP's JWKS), and sets a **signed, HttpOnly session cookie** (itsdangerous,
`TRIAGE_SESSION_SECRET`, TTL `TRIAGE_SESSION_MAX_AGE`, default 8h). `OidcAuth.authenticate`
resolves that cookie to a `Principal`, materializing the identity in `registry.users` exactly
as TrustedHeaderAuth does; admin stays `TRIAGE_ADMIN_EMAILS` — with **no default admin**
(unlike the laptop backend: a shared deployment must configure admins explicitly).

API-vs-browser is kept distinct: an unauthenticated **API** request gets a **401 JSON** whose
detail carries `login_url` (the SPA redirects itself); only the browser endpoints redirect.
Configuration is env-only, fail-fast naming the variable (`TRIAGE_OIDC_ISSUER`,
`TRIAGE_OIDC_CLIENT_ID`, `TRIAGE_OIDC_CLIENT_SECRET`, `TRIAGE_SESSION_SECRET`) — the
credential hard rule. The dependency surface (`authlib`, `itsdangerous`, `httpx`) is the
optional `triage[oidc]` extra; `TRIAGE_AUTH=oidc` without it fails loud naming the install.

## Considered alternatives

- *Proxy-injected identity (oauth2-proxy/nginx in front + TrustedHeaderAuth)* — viable today
  with zero code, and remains the documented alternative for always-behind-a-proxy
  deployments. Rejected as the primary: it moves auth correctness outside the repo (untestable
  in the suite), and every deployment must assemble the proxy correctly or silently run open.
- *Hand-rolled JWT validation* — rejected: JOSE/JWKS subtleties are exactly where DIY auth
  fails; authlib's `jose` is the narrow, audited piece we take.
- *Server-side sessions in the registry DB* — rejected for v1: a signed cookie needs no new
  table/GC; revocation-on-demand (the one thing DB sessions buy) can arrive later behind the
  same seam.

## Consequences

- The IdP round-trip functions (`fetch_discovery`, `exchange_code`, `decode_id_token`) are
  module-level seams — tests stub them; no live IdP in the suite.
- The session is bearer-by-cookie: deploy behind HTTPS (the cookie sets `secure` on https
  schemes) and rotate `TRIAGE_SESSION_SECRET` to force global logout.
- `GET /api/me` now reports `auth_mode`, so the SPA renders logout only when it exists.
