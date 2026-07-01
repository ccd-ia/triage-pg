# 0024. Write webapp: POST routes over the registry, a pluggable user-auth seam

- Status: Accepted
- Date: 2026-07-01
- Deciders: Adolfo, Claude

The read dashboard (ADR-0012/0021, `docs/read-dashboard-spec.md`) is a read-only JSON API over
the in-PG views; the spec deferred the **write webapp** (config submission + project/user
management) as "a separate, later surface with its own ADR, gated on the multi-project registry
(ADR-0002)." This ADR is that record. It adds a *write* half to the existing FastAPI app — create
control-plane rows and submit experiments — without a second service and without moving business
logic into the UI.

## Decision

**1. The write surface extends the existing app; it is not a new service.** A second
`APIRouter` (`triage.dashboard.write_routes.write_router`) mounts under the same `/api` prefix as
the read router. One app, two routers: read = `SELECT` over `triage.*` views, write = control-plane
mutations + experiment submission. Handlers stay as thin as the read half — auth → a
`triage.registry` call and/or the injected runner (ADR-0012: submitting here calls the **same**
`run_experiment` the CLI does; no logic is duplicated in the webapp).

**2. Two pools, addressed by role.** The app now holds a **registry** pool (the ADR-0002 control
plane — `registry.projects/users/project_members/submissions`) alongside the existing **project**
pool (the bound results DB an experiment runs against). The registry pool is **optional**: the read
dashboard runs without it, so a missing `TRIAGE_REGISTRY_URL` (and no injected `registry_pool`) is
not an error — the write routes `503` until one is configured. `triage.registry` is the psycopg3
access layer over the control plane, in the same shape as `triage.sources`.

**3. A user-auth seam distinct from the profile DB-auth adapter.** WS5's auth is *human↔webapp*
identity (who is calling, are they an admin) — deliberately **separate** from
`triage.profiles.AuthAdapter`, which is *machine↔database* auth (password locally, RDS-IAM in
cloud). The seam is one `AuthBackend` protocol resolved into a `Principal` by one dependency
(`current_principal`); routes only ever see a resolved `Principal`. v1 ships `TrustedHeaderAuth` —
a local/single-tenant backend that trusts an `X-Triage-User` header (or a dev default) and
materializes the identity in `registry.users`. It is explicitly **not** real authentication;
real IdP-backed auth (OIDC / session / SSO-mapped identity) is a drop-in replacement for that one
class, selected by `TRIAGE_AUTH`. An unknown `TRIAGE_AUTH` fails loud (never degrades to
trust-everything).

**4. Submission = authz → run via the profile seam → append an audit row.** `POST /api/submissions`
resolves the target project, checks membership (admins anywhere; otherwise owner/contributor),
shape-validates the config, then calls the **injectable** experiment runner
(`app.state.experiment_runner`, default `default_experiment_runner`) which runs in-process locally
or submits one AWS Batch job in cloud (ADR-0005) — the same execution seam as `triage run`. The
`registry.submissions` row is the append-only audit trail (who submitted what, where it routed).
Injectability keeps the route testable without a real training run.

**5. v1 binds one project DB per app instance.** The submission runs against the app's bound
project pool; the registry records each project's `database_name` but per-project-DB *routing*
across many databases (opening a pool per project on demand — the full ADR-0002 multi-tenant
cluster) is deferred. The seam is ready for it (the registry already stores `database_name`); the
routing is the next step.

## Considered alternatives

- *A separate write service* — rejected: needless operational surface; the read app already owns
  the project pool, the SPA static mount, and the FastAPI lifespan. One app, two routers is simpler.
- *Reuse the profile `AuthAdapter` for user identity* — rejected: it produces a database pool, not a
  human identity; conflating DB-auth with webapp-auth would couple RDS-IAM to login and block a
  plain OIDC swap.
- *Real auth (OIDC) in v1* — rejected as scope: the local profile needs a usable write surface now;
  `TrustedHeaderAuth` behind the seam gives that, and real auth lands later without touching routes.
- *Submit asynchronously via a job queue* — deferred: local submission runs in-process (blocks the
  request); cloud already returns immediately with a Batch job id. A background queue for local is a
  later enhancement, not a v1 requirement.

## Consequences

- New modules: `triage.registry` (control-plane access), `triage.dashboard.auth` (the user-auth
  seam), `triage.dashboard.write_routes` (projects + submissions); `registry_schema.upgrade_registry_db`
  helper. `create_app` gains optional `registry_pool` / `auth_backend` / `experiment_runner`
  injectables; the lifespan opens the registry pool from `TRIAGE_REGISTRY_URL` when present.
- The write surface is inert until a registry is configured (503), so the read dashboard and its
  tests are unaffected.
- `TrustedHeaderAuth` trusts its header — deploy it only on a laptop or behind a trusted proxy; a
  shared/public deployment must configure a real `AuthBackend` (and `TRIAGE_ADMIN_EMAILS`).
- Per-project-DB routing and async local submission remain open, tracked for the multi-project /
  cloud track.
