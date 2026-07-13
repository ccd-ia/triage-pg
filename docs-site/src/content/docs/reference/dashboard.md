---
title: The dashboard, surface by surface
description: A screenshot-led walk of every dashboard view — what question each panel answers and which table feeds it.
sidebar:
  order: 2
  label: Dashboard tour
---

The dashboard is a deliberately thin window: every number you'll see is a
`SELECT` over the views and functions in the `triage` schema (ADR-0012 — no
business logic in the UI), so anything shown here is equally scriptable from
[the CLI](/triage-pg/reference/cli/) or `psql`. Start it with `just serve`
against any project database.

## Experiments — the front page

![The experiments list: one row per experiment with problem type, framing, groups, models, base rate, last run](../../../assets/tutorials/experiments-list.png)

One row per **experiment** (= prediction problem, ADR-0022): problem type,
observation-regime pill (`early warning` / `resource prioritization` /
`visit level`), model/group counts, base rate, last run status. The question
it answers: *what problems does this project attack, and are they healthy?*

## The experiment overview

![The experiment overview: header chips, per-split sparklines, the model-groups × splits heatmap](../../../assets/tutorials/experiment-overview.png)

The working view. The header carries the identity chips (hash, problem type,
framing) and the four per-split sparklines — cohort size, labels, %-labeled
(framing-aware: an inspections problem *expects* <100%), base rate. Below,
the **heatmap**: model groups × temporal splits, best-in-split outlined —
the "which model family is winning, and is it stable over time?" panel. A
population selector re-scopes everything to a named subset when subset
evaluations exist.

## The model card

![A model card: threshold curves, score histogram, calibration deciles, feature importances](../../../assets/tutorials/model-sheet.png)

One model's dossier. The **threshold curve** is the operational panel —
precision/recall as you sweep the list size k, i.e. "if we can act on the top
k, what do we get?". Score histogram, calibration deciles, persisted feature
importances, and the postmodeling panels (crosstabs, error tree) when
`triage postmodel` has run.

## Audition — model selection with discipline

![The audition tab: DSSG's selection rules computed in PostgreSQL](../../../assets/tutorials/audition-tab.png)

DSSG's selection rules (distance-from-best, max regret, regret-next-time…)
computed as SQL views. The question: *which model group would we actually
deploy* — the one that's never far from best across time, not the lucky
winner of one split. When the leaderboard's #1 and audition's pick disagree,
the context bar flags it.

## Bias — fairness with a guide

![The bias tab: per-group metrics with disparity ratios and τ verdicts, and the fairness-tree wizard](../../../assets/tutorials/bias-tab.png)

Per-protected-group metrics over the top-k list with disparity ratios and
τ-verdicts, straight from the SQL `bias_metrics` table (Aequitas' math, no
Python runtime). The **fairness-tree wizard** asks the two Aequitas questions
(punitive or assistive? intervene on all flagged?) and highlights the metric
family your intervention actually implicates.

## Model groups

![The model-groups table: avg ± σ, max regret, fit time per group](../../../assets/tutorials/model-groups-table.png)

The hyperparameter-family rollup (avg ± σ, max regret, fit time) — and where
feature-group ablation runs (ADR-0023) become comparable side by side.

## Monitoring

![The monitoring view: PSI/KS drift chips, scoring-volume heartbeat, realized outcomes over time](../../../assets/tutorials/monitoring-view.png)

The post-deployment view over the append-only predictions history: score
drift (PSI/KS chips), the scoring-volume heartbeat, and realized outcomes as
labels mature. The purpose chip is the provenance honesty marker —
`experiment` rows are backtest history, `forward_score` rows are production.

## Projects & Submissions — the write surface

![The projects view: the registry control plane](../../../assets/tutorials/projects-view.png)

With a registry configured (`TRIAGE_REGISTRY_URL`), **Projects** manages the
one-database-per-project lifecycle and **Submissions** accepts experiment
configs through the same validator the CLI uses — dry-run verdicts with
path-addressed errors before anything runs:

![The submissions form: config validation with path-addressed errors before submission](../../../assets/tutorials/submissions-form.png)

Without a registry, both render a neutral read-only notice — a supported
deployment, not an error.

## Where next

- The [entity drawer and derivation graph](/triage-pg/tutorials/dirtyduckling/)
  appear in the tutorials' guided tours.
- [Architecture](/triage-pg/reference/architecture/) explains the tables all
  of this reads.
