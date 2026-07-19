# ADR conformance audit

- Date: 2026-07-03
- Auditor: claude-fable-5 (Claude Code), per `specs/triage-pg-v1-completion.html` Phase 1
- Method: every ADR re-read against the tree at `76eb46fc`; evidence spot-verified by
  running the cited greps/tests, not by trusting status lines. The full suite was run
  green at the end of the audit (see Verification at the bottom).

Verdicts: **fulfilled** (implemented + evidence verified) · **partial** (a named gap
remains) · **holding** (a deliberate deferral that still holds) · **code-complete,
unexercised** (built + mock-tested, never run against the real environment).

| ADR | Decision (short) | Verdict | Evidence |
|------|-----------------|---------|----------|
| 0001 | Clean fork, fresh repo, greenfield schema | fulfilled | `git remote -v`: origin=`ccd-ia/triage-pg`, dssg push-disabled; greenfield baseline `results_schema/alembic/versions/0001_initial_triage_schema.py` |
| 0002 | DB-per-project + registry control plane | fulfilled (closed 2026-07-03) | Registry schema: `registry_schema/alembic/versions/0001_initial_registry_schema.py`; routing: `dashboard/project_routing.py` + `dashboard_tests/test_project_routing.py` (ADR-0025). The lifecycle gap found by this audit was closed the same day (plan Phase 2): `triage project create/drop/list`, `src/triage/project_lifecycle.py`, registry migration 0002, `src/tests/test_project_lifecycle.py` |
| 0003 | Plain PG, two profiles behind three adapters | fulfilled | `triage/profiles/{__init__,protocols,auth,storage,execution}.py`, fail-fast `load_profile`; `src/tests/test_profiles.py` (mocked AWS); spec `docs/cloud-profile-spec.md` |
| 0004 | Cloud auth = RDS IAM tokens | **code-complete, unexercised** | `profiles/auth.py` `CloudAuth` (`generate_db_auth_token`, per-connection token, `verify-full`); moto/stub-tested only — never run against live RDS → plan Phase 5 (gated) |
| 0005 | Execution = one AWS Batch job per experiment | **code-complete, unexercised** | `profiles/execution.py` `BatchExecution`; rq/multicore removed (grep: no `multiprocessing` under `adapters/`); no live Batch run, no `describe_jobs` status poll → plan Phase 5 (gated) |
| 0006 | Append-only, timestamped, partitioned predictions | fulfilled | Migration 0001: `partition by range (scored_at)` (line 230, quarterly); append-only asserted by `adapter_tests/test_run_orchestration.py:353`. Monitoring *features* still deferred → plan Phase 6 |
| 0007 | Evaluation + leaderboards + bias in PostgreSQL | fulfilled (with recorded waiver) | Migration 0002 PL/pgSQL metrics validated vs sklearn (`catwalk_tests/test_in_pg_metrics.py`); bias group-bys + disparity vs reference validated on hand-computed fixtures matching Aequitas' definitions (`test_bias_metrics_group_by_and_disparity`, `test_bias_metrics_explicit_reference_group`). Direct Aequitas-output parity **waived** — Aequitas is pandas-2-incompatible and cannot run to produce references (the very reason it was dropped); waiver recorded on the ADR. Flag #1 (audition dual surface) closed 2026-07-05: `component/audition` retired, SQL+CLI parity via migration 0013 (see Flags). Update 2026-07-06 (v1-release plan P2–P5): the bias metric set is COMPLETE (fnr/for/npv + τ verdicts, migration 0014; `bias_config` ingestion end-to-end) and subset evaluation FILTERS (migration 0015 — the schema-design §8.6 recorded-only deferral is resolved; the subset is the population, `test_subsets.py` hand-computed parity) |
| 0008 | featurizer replaces Collate | fulfilled | Pin `pyproject.toml:31` = `featurizer[parquet] @ …@v0.9.1`; `component/collate` gone; scale validated (`docs/featurizer-scale.md`), re-validated v0.4.1/v0.8.0; v0.9.x additive families reachable via passthrough (`docs/featurizer-0.9-features.md`) |
| 0009 | Imputation split: fit-free vs fit-based (train-only) | fulfilled | `adapters/imputation.py` (+ `adapter_tests/test_imputation.py`); train-only stats persisted to `matrices.metadata`, reused by test via the train-matrix parent edge (`adapter_tests/test_matrix_assembler.py` property tests); categorical-encoding extension implemented (`cat_encodings`) |
| 0010 | problem_type ranking spine; survival-ready labels | **partial (by design)** | Discriminator + spine implemented; `labels.duration`/`event_observed` in migration 0001 (lines 203–204). **Gaps:** no survival train/eval path, no `c_index` anywhere (grep-verified); `metric_config` reaches `in_pg_evaluation.py` (incl. an unused `DEFAULT_REGRESSION_CONFIG`) but is not selectable from the experiment YAML → plan Phase 3 |
| 0011 | No standalone postmodeling module | fulfilled | Module removed (only a stale gitignored `component/postmodeling/__pycache__/` remains on disk — harmless debris, flagged for manual cleanup); importances persisted at train (`adapters/model.py`; β/odds via migration 0009) |
| 0012 | Headless-complete core; thin UIs later | fulfilled | CLI + SQL views complete; read dashboard (`dashboard/` + `frontend/`); write webapp shipped (ADR-0024). ADR status line was stale ("in progress") — corrected 2026-07-03 |
| 0013 | Artifact identity = derivation hash over the closure | fulfilled | `triage/derivation.py` (dual Merkle, `as_uuid`); `artifacts`/`artifact_inputs`/`run_artifacts` schema; `src/tests/test_derivation.py`, `test_artifacts.py` |
| 0014 | Source pins enter identity | fulfilled | `triage/sources.py` + `triage source` CLI; `run_source_pins`; unpinned-source volatile path; `src/tests/test_sources.py` |
| 0015 | DAG node granularity; stops at models | fulfilled | Per-(config, as_of_date) nodes in `adapters/{cohort,labels}.py`; test matrix takes train matrix as parent; cross-run cache reuse in `adapter_tests/test_run_orchestration.py` |
| 0016 | Engine versions in identity; logical fallback | fulfilled | `derivation.engine_versions_for()`; `logical_id` + ENGINE-DRIFT warning; featurizer release version hashed into feature_group identity |
| 0017 | GC collects outputs, not history | fulfilled | `triage gc`/`triage archive` CLI; `artifacts.collect()` → storage-adapter deletion; RESTRICT FK hardening in migration 0001; `src/tests/test_gc.py` |
| 0018 | Retrain provenance on `runs` | fulfilled | Migration 0003 (`runs.purpose`, `runs.prediction_date`); `adapters/{retrain,forward}.py`; `adapter_tests/{test_retrain,test_forward}.py` |
| 0019 | psycopg3-native app code; SQLAlchemy only behind alembic | fulfilled | grep `sqlalchemy` under `src/triage` (excl. alembic zones): the only hits were two stale *docstrings* in `in_pg_evaluation.py` (`db_engine (sqlalchemy.engine.Engine)` on a function that takes a `ConnectionPool`) — corrected in this audit; no imports |
| 0020 | Grid×split loop stays serial | holding | No `multiprocessing` under `adapters/` (grep-verified). Re-confirmed against the upcoming monitoring workload: scheduled forward scores are single-model invocations, inherently serial — the deferral still holds |
| 0021 | Live progress: pg_notify → SSE + REST poll | fulfilled | `artifacts._notify_run_progress` emitted at artifact/run/eval transitions (`adapters/run.py:364,386,474`, `in_pg_evaluation.py`); SSE `dashboard/routes.py` `/api/runs/{id}/stream`; `runs.plan` (migration 0004) |
| 0022 | Experiment = the problem; runs are attempts | fulfilled | `adapters/run.py:168` `_problem_identity` / `:178` `experiment_hash_for` (hashes cohort+label+temporal+problem_type only); `adapter_tests/test_experiment_identity.py`; food DB re-seeded to one experiment (2026-06-26) |
| 0023 | Feature groups + four strategies | fulfilled | `adapters/feature_groups.py` (partition + all/leave-one-out/leave-one-in/all-combinations, capped); `adapter_tests/test_feature_groups.py`; fan-out into Runs under one Experiment |
| 0024 | Write webapp: registry POSTs + user-auth seam | fulfilled | `triage/registry.py`, `dashboard/auth.py` (`Principal`/`AuthBackend`/`TrustedHeaderAuth`), `dashboard/write_routes.py`; `dashboard_tests/test_write_api.py`; commits `b657b17f` (backend) / `e9310683` (frontend). Status line added 2026-07-03 |
| 0025 | Per-project DB routing — project switcher | fulfilled | `dashboard/project_routing.py` (`resolve_active_pool`/`pool_for_slug`/`project_dburl`); `dashboard_tests/test_project_routing.py`; live-proven across the 3 tutorial DBs (commit `76eb46fc`). Status line added 2026-07-03. Open refinement: full reload on switch (deep-link fix → plan Phase 2) |

## Contradiction found (1)

`docs/cloud-profile-spec.md` resolved-decision 3 states the inherited
`catwalk/storage.py` `Store` **"is retired"**, while the same document's
implementation note (2026-06-21) records that it was **not** retired —
`cli.py` (`Store.factory` config loading) and four test modules still imported it
(re-verified 2026-07-03). **Closed the same day** (plan Phase 2): the CLI config
loaders now read through `profiles.storage.storage_for_root` (preserving the cloud
`s3://` config path), `catwalk/storage.py` + `util/pandas.py` and their tests are
deleted, and the spec's §3 status note records the closure.

## Out-of-scope check

`.out-of-scope/deep-learning.md` is honored: no `torch`/`tensorflow`/`keras`
anywhere in `pyproject.toml` or `src/` (grep-verified 2026-07-03). Note for
Phase 3: `scikit-survival` (classical survival analysis) does **not** violate
this exclusion.

## Flags for the maintainer (decisions, not defects)

1. **Audition dual surface.** ~~Needs a call.~~ **CLOSED 2026-07-05 — retired**
   (maintainer decision, v1-release plan P1). `component/audition/` and
   `audition_tests/` are deleted; migration 0013 brings the SQL surface to full
   parity (`dist_from_best_case_next_time` + `avg/max_regret_next_time`, DSSG
   semantics) and `triage audition` / `triage leaderboard` are Rich-table CLI
   reads over the same views the dashboard uses (ADR-0012 headless parity; the
   8-rule catalog lives once in `in_pg_evaluation.AUDITION_RULES`). The original
   flag text: the inherited Python module (AuditionRunner, ~15 modules incl.
   `plotting.py` and a notebook) survived as a second audition surface,
   repointed at greenfield tables — accept-or-retire was the open question.
2. **Stale `component/postmodeling/__pycache__/`** on disk (gitignored, no
   sources). Deleting it is a one-liner left to the maintainer.
3. **ADR-0020 re-confirmed** for the monitoring track (serial is correct for
   single-model forward scores); no change needed.
4. **Planned ADRs 0026–0028** each pass the three-criteria bar (hard to
   reverse / surprising without context / real trade-off): 0026 survival
   (estimator library + C-index locus bind config surface and eval identity),
   0027 monitoring (no-daemon scheduling vs pg_cron vs a worker is a real
   architectural fork), 0028 OIDC (in-app flow vs proxy-injected identity).
   Each ADR will record its criteria check in its Context section.

## Verification

- `just test` — full suite green after the audit's two hygiene edits (stale
  docstrings in `in_pg_evaluation.py`); count recorded in the plan's Phase 1
  checklist.
- `grep -L 'Status' docs/adr/00*.md` — empty: every ADR carries a status line.
- This document: 25 ADR rows (`grep -c '^| 00'`).
