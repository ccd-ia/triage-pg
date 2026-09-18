# 0032. A separate, identity-neutral cohort for forward scoring

- Status: Accepted — **Implemented** 2026-09-17 (branch `claude/close-open-issues`, issue #10)
- Date: 2026-09-17
- Deciders: Adolfo De Unánue

## Context

`predict_forward` rebuilt the cohort at a new `as_of_date` from
`lineage.cohort_config["query"]` — the **training** cohort, verbatim — and offered no
override on `predict_forward`, `predictlist` or the `score` CLI command.

A supervised cohort normally selects **outcome-bearing** rows. It usually has to: the
label query needs a realized outcome, so the cohort is written to return only entities
whose outcome is knowable. The reporter's is typical:

```sql
where w.game_date = {as_of_date}::date
  and w.game_state in ('OFF', 'FINAL')     -- drops every unplayed game
```

Reusing that query at scoring time therefore excludes, **by construction**, exactly the
entities forward scoring exists to score. The truncation is silent: the run completes, the
predictions table gains rows, and the rows that mattered are simply absent.

The entities to score are not a property of the *problem*. An experiment is its cohort,
label and temporal config (ADR-0022); the production cohort says which entities a *later*
scoring run visits, which is an observation of the problem rather than part of it. That
distinction already has a precedent in this codebase: `task_framing` (migration 0019) tags
the observation regime and is identity-neutral by construction.

Two shapes were considered. A CLI flag alone keeps the identity question from arising at
all, but leaves the production cohort living in whatever crontab invokes the scorer, where
it is invisible to the artifact DAG and to anyone reading the config. A config key records
it with the problem it belongs to — at the cost of having to be explicitly excluded from
the experiment hash, because `_problem_identity` hashes the whole `cohort_config` mapping.

## Decision

Add an optional **`cohort_config.forward_query`**, and make it **identity-neutral**.

- `_problem_identity` strips `forward_query` from `cohort_config` before hashing
  (`_COHORT_IDENTITY_NEUTRAL_KEYS`). A config that adds the key keeps its existing
  `experiment_hash`.
- `validate_experiment_config` holds it to the same `{as_of_date}` placeholder rule as the
  training query, since `build_cohort` renders both identically.
- Precedence at scoring time: an explicit `--cohort-query` override, then `forward_query`,
  then the training `query`. The training cohort remains the fallback, so a config that
  does not set the key scores exactly the rows it scored before.
- `triage score` and `triage predictlist` take `--cohort-query PATH`. A path, not an inline
  string: a cohort query is multi-line SQL containing quotes, and shell quoting is how it
  gets mangled.

## Consequences

**The identity-neutrality is the load-bearing part, and it cuts both ways.** Two
experiments differing only in `forward_query` share an `experiment_hash` and are the same
Experiment. That is what makes the key safe to add — had it entered identity, every
experiment in every existing database would have forked the day it shipped, and every
`leaderboard`/`audition` row keyed on the hash would have split with it. The price is that
the config alone no longer tells you which production cohort a given forward score used;
the *artifact* does, because `build_cohort` derives the cohort node from the rendered
query, so two different forward cohorts are different cohort artifacts under one
experiment.

**A forward cohort that returns unresolved entities produces label rows with NULL
outcomes.** That was already true and is why the production matrix left-joins labels
(`forward.py`, G1) — but it becomes the common case rather than the edge case, so a
forward-scored run will routinely carry labels that evaluate to nothing until the outcomes
land. Evaluation is unaffected: it runs later, over the append-only predictions, once
outcomes exist (ADR-0006, ADR-0027).

**Adding the key is not retroactive.** Models already trained keep scoring over the
training cohort until their config gains a `forward_query`, or the operator passes
`--cohort-query`. No backfill is possible or attempted — the production cohort is a
statement about intent, which a migration cannot infer.
