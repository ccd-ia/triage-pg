# 0001. Clean fork: fresh repo, greenfield schema, no upstream tie

- Status: Accepted
- Date: 2026-06-04
- Status update (2026-06-28): Implemented — fresh `ccd-ia/triage-pg` repo + greenfield alembic baseline (`0001_initial_triage_schema.py`); no migration path from old triage DBs.

triage-pg is a from-scratch simplification of DSSG's `triage` for a PostgreSQL-native, teaching-and-consulting use case. We start a **fresh repository** under the `ccd-ia` org (seeded from the modernized `triage` tree at commit `f7366d7b`, git history + attribution preserved) rather than a GitHub fork, and a **greenfield results schema** with **no migration path** from existing triage databases. Because the clean-schema rewrite is a hard divergence (postmodeling rewritten, rq removed, schema redesigned), upstream commits won't apply cleanly and we won't send PRs back — so the only thing a GitHub fork buys (pulling upstream / contributing back) is worthless here, while a plain repo gives us Issues, privacy, and independence.

## Considered alternatives
- *GitHub fork of dssg/triage* — rejected: the fork relationship is dead weight under a hard divergence; forks also disable Issues by default, can't be made private, and need GitHub support to detach.
- *In-place modernization of dssg/triage* — already attempted (PR #994, since abandoned); the rebase/CI/back-compat friction is exactly what the fork escapes.
- *Importer from old triage result DBs* — rejected: nothing needs carrying forward, and an import would be lossy (no `scored_at` history, single-namespace→project remap).

## Consequences
- Old triage result databases remain readable only by old triage.
- Provenance preserved via retained git history + LICENSE/NOTICE.
