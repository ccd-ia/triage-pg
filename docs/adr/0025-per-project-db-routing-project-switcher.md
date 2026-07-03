# 0025. Per-project database routing — the project switcher

- Status: Accepted
- Date: 2026-07-02
- Deciders: Adolfo, Claude
- Status update (2026-07-03): Implemented — `dashboard/project_routing.py` (`resolve_active_pool`/`pool_for_slug`/`project_dburl`), top-bar `ProjectSwitcher` (commit `76eb46fc`); live-proven across the three tutorial DBs. The stale deep-link 404 is fixed (v1-completion plan Phase 2): switching navigates to the project-neutral `/experiments` list instead of reloading in place. Context-driven panel refresh (no full reload) stays deferred.

The read dashboard binds ONE project database per app instance (ADR-0012); the registry
(ADR-0002) lists many projects, each isolated in its own database. ADR-0024 deferred *routing*
across those databases. This ADR adds it: one dashboard instance can serve any registry project,
selected per request — the "project switcher". The hard question it settles is **how a request
gets a connection to project X's database when the registry deliberately stores no credentials**.

## Decision

**1. The active project is a request header, resolved to a pool through the one `_pool` seam.**
Every read handler already depends on a single `_pool(request)`; it now delegates to
`resolve_active_pool`, which reads an ``X-Triage-Project: <slug>`` header. No header (or no
registry) ⇒ the app's bound pool, so single-project use is byte-for-byte unchanged. The frontend
stores the active slug in `localStorage` and sends the header on every request; switching reloads
so all panels re-fetch against the new project.

**2. Credentials never enter the registry (ADR-0002); a project's database URL is resolved two
ways, in order:**
- **`TRIAGE_PROJECT_DB_MAP`** — an optional env JSON `{slug: url}`. This is the *local profile
  "uses env"* path, needed when projects live in **separate clusters/containers** (as the tutorial
  dockers do — food:5435, donors:5437, chi311:5438). The registry table stays credential-free: the
  map is environment, not data.
- **dbname swap on the base connection** — take the app's base project URL (host/port/creds) and
  replace only the database segment with the registry's `database_name`. This is the ADR-0002
  **shared-cluster** path and the **cloud** path (one RDS endpoint + IAM, a database per project on
  the same cluster). No per-project secret — same credentials, different database.

**3. Pools are opened on demand and cached per distinct URL** (`app.state.project_pools`), closed at
shutdown. When a project resolves to the app's own bound database, the default pool is reused (no
duplicate) — so the common "instance bound to project X, registry also lists X" case opens nothing
extra.

**4. Fallback vs. fail, deliberately.** An **unknown** slug (a stale `localStorage` selection) falls
back to the default pool — benign, the switcher just re-lists. A **known** project that can't be
routed (no map entry and no base URL to swap onto) raises **503** — a real config error, never
silently serving another project's data. Submissions (ADR-0024) route the same way: a submit runs
against its **target** project's database, not merely the bound one.

## Considered alternatives

- *Store a connection URL per project in the registry* — rejected: violates ADR-0002 (the registry
  holds no credentials); would put secrets in a shared control-plane table.
- *One dashboard process per project (no switcher)* — rejected: the user asked for a switcher; N
  processes + N ports is worse operationally than one instance routing by header.
- *Path-prefix the project (`/api/projects/{slug}/…`)* — rejected for v1: it would rewrite every
  read route and the SPA's routing; a header is a minimal, orthogonal seam over the existing routes.
- *Per-request pool with no cache* — rejected: opening a pool per request is wasteful; the small
  per-URL cache is bounded by the number of projects.
- *Diff-based panel refresh on switch instead of a full reload* — deferred: the panels are many
  independent reads; a full reload is the simplest correct v1. A shared active-project context that
  invalidates panels is a later refinement.

## Consequences

- New `triage.dashboard.project_routing` (`resolve_active_pool`, `pool_for_slug`, `project_dburl`);
  `routes._pool` delegates to it; `create_app` gains `app.state.{project_pools, base_project_url}`
  and closes the cached pools at shutdown. Frontend: `getActiveProject`/`setActiveProject` +
  `X-Triage-Project` on every request, and a top-bar `ProjectSwitcher`.
- Local multi-project demo needs `TRIAGE_PROJECT_DB_MAP` (the tutorial DBs are separate clusters);
  a real shared cluster / cloud RDS needs nothing extra (dbname-swap on the base connection).
- The switcher only appears when a registry is configured and lists projects; otherwise the
  dashboard is single-project exactly as before.
- Open: the SPA does a full reload on switch (v1); a context-driven refresh is a later refinement.
