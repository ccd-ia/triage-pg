# Contributing to triage-pg

Thanks for helping out! triage-pg is a PostgreSQL-native, deliberately simplified
fork of DSSG's [triage](https://github.com/dssg/triage) — see
[`docs/triage-pg-vs-dssg-triage.html`](docs/triage-pg-vs-dssg-triage.html) for how
the two relate. Issues and pull requests live at
<https://github.com/ccd-ia/triage-pg>.

## Before you write code

The design is captured in committed, durable artifacts. Reading the relevant one
first will save you a round-trip in review:

- **[`CONTEXT.md`](CONTEXT.md)** — the domain glossary (Project, Experiment, Run,
  as_of_date, Cohort, Matrix, …). Use these terms exactly, in code and prose.
- **[`docs/adr/`](docs/adr/)** — 28 accepted architecture decisions. If your change
  contradicts one, open an issue first; if it *implements* one, cite it in the PR.
- **[`.out-of-scope/`](.out-of-scope/)** — features we have explicitly rejected
  (e.g. deep learning). Check here before proposing scope additions.
- **[`docs/README.md`](docs/README.md)** — the docs index (mental model, tutorials,
  specs).

The cardinal rule of the codebase is **point-in-time correctness**: features for an
`as_of_date` may only use data knowable strictly before it. If your change touches
cohorts, labels, features, imputation, or matrices, review ADR-0009 (the
fit-free/fit-based imputation split) and the "Common Gotchas" the docs describe —
leakage bugs are the ones we care most about catching in review.

## Development environment

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/),
[just](https://github.com/casey/just), a local PostgreSQL **server executable**
(the test suite starts and tears down its own throwaway clusters via
`pytest-postgresql` — you don't need a running server or any databases), and
Docker (only for the tutorial databases and the dashboard image).

```bash
git clone git@github.com:ccd-ia/triage-pg.git
cd triage-pg
uv sync --extra dev          # creates .venv with locked versions
just --list                  # the recipe catalog — start here
```

Optional extras: `--extra survival` (scikit-survival), `--extra dashboard`,
`--extra oidc`. CI installs all of them.

## Everyday commands

```bash
just test                    # full suite (pytest; spins throwaway Postgres)
just test src/tests/adapter_tests/ -v   # a single directory or file
just lint                    # ruff check
just typecheck               # basedpyright (a pre-existing error backlog exists;
                             #   don't add to it — new code should be clean)
just serve                   # the dashboard (read views + write surface)
just docs-site               # Astro Starlight docs site, hot reload
just tutorial-up             # DirtyDuck tutorial DB   (Docker, port 5435)
just donors-up               # DonorsChoose tutorial DB (port 5437)
just chi311-up               # Chicago 311 tutorial DB  (port 5438)
```

Frontend work lives in `frontend/` (React + Vite): `npm run build` must pass tsc
strict, and `npm run lint` must be clean.

## Testing conventions

- Test location mirrors source: `src/tests/adapter_tests/` ↔ `src/triage/adapters/`,
  `src/tests/dashboard_tests/` ↔ `src/triage/dashboard/`, etc.
- DB-backed tests get a pooled greenfield database from the `db_pool_greenfield`
  fixture (see `src/tests/conftest.py`); reusable helpers (a known-good sample
  config, source seeding) live in `src/tests/utils.py`.
- Tests run only against **plain standalone PostgreSQL** (ADR-0003) — no
  proprietary extensions, nothing cloud-only. Cloud seams are tested against
  stubs/`moto`, never live AWS.
- New behavior needs a test that fails without the change. Bug fixes need a
  regression test that pins the bug.

## Code style

- `ruff` is the formatter and linter (line length 88); a pre-commit-style hook
  formats on save in most setups, and CI enforces it.
- Type hints are required (`from __future__ import annotations`); docstrings are
  Google-style. Log through `triage.logging.get_logger(__name__)` (loguru).
- SQL: lowercase keywords, CTEs for multi-step queries, explicit join types.
- Raw SQL through SQLAlchemy connections needs `text()`; psycopg3 code follows
  the patterns already in `src/triage/adapters/` (COPY, NaN→NULL, intervals).

## Config validation

Experiment configs are validated by
`triage.adapters.run.validate_experiment_config` — the single dry-run validator
behind both `triage analyze-config` and the webapp's `POST /api/validate-config`
(ADR-0012: validation is core logic, not UI logic). If your change adds or renames
a config key:

1. Teach the validator about it (errors are path-addressed; unknown top-level
   keys warn).
2. Update the example configs in `example/*/experiment*.yaml` — they are validated
   verbatim by the test suite and served by the dashboard's example picker.
3. If the key affects experiment identity, stop: identity is fixed by ADR-0022
   (an Experiment is the *problem* — cohort+label+temporal+problem_type). Changes
   there need an ADR-level discussion first.

## Documentation

- `docs/*.md` are plain Markdown rendered on GitHub — update them in the same PR
  as the behavior they describe.
- The public docs site lives in `docs-site/` (Astro Starlight, published to
  <https://ccd-ia.github.io/triage-pg/>). Tutorial pages are **verbatim-verified**:
  every command block is expected to run as written against a fresh tutorial
  stack, so if you change CLI output or flags, re-run the affected tutorial
  commands and update the page.
- Architecture decisions that are hard to reverse, surprising without context,
  *and* the result of a real trade-off get a short ADR in `docs/adr/` (next
  number, same format). Most changes don't.

## Pull requests

1. Branch from `main` (`git switch -c your-feature`). `main` is protected; work
   lands via PR.
2. Keep PRs small — the minimal change that adds value reviews fastest.
3. Before opening: `just test && just lint` green, frontend build/lint green if
   you touched `frontend/`, docs updated alongside behavior.
4. CI (`.github/workflows/ci.yml`) runs five jobs — tests, lint, frontend build,
   terraform validate, docker build — on every PR with no secrets; a red build
   blocks merge.
5. Use a descriptive title and reference the issue (`Resolves #123`) so the merge
   closes it.

## Reporting bugs

File issues at <https://github.com/ccd-ia/triage-pg/issues> with your OS,
PostgreSQL version, the exact command, and steps to reproduce. For anything
touching temporal correctness (features/labels/matrices), include the experiment
config — the temporal blocks are usually where the story is.
