---
title: Concepts
description: The mental model behind triage-pg — the pipeline, and the four ideas that make its numbers trustworthy — with a map to the deeper pages.
sidebar:
  order: 0
---

The tutorials show you *how* to run triage-pg; this section explains *why* it
works the way it does. Read it once and the rest of the docs — every config key,
every gotcha — stops being arbitrary.

## The pipeline

Every experiment walks the same path, whatever the dataset or problem:

```text
cohort ──▶ features ──▶ matrices ──▶ train ──▶ predict ──▶ evaluate
(who, as   (point-in-  (Parquet)    (per      (append-    (in
 of when)   time)                    split)    only)       Postgres)
```

You declare this once as an **Experiment** (an `experiment.yaml`); each **Run** is
one attempt at it. What entities enter, what they're labelled with, what features
describe them, and how the model is scored are all config — the machinery is fixed.

## The four ideas

Everything else on this site is a consequence of four decisions. Each has its own
page:

- **[Point-in-time correctness](/triage-pg/concepts/point-in-time-correctness/)** —
  the cardinal rule: a feature for an `as_of_date` may use only data knowable
  *strictly before* it. This is what separates an honest offline number from a
  leaky one, and it's why imputation is split the way it is.
- **[Identity &amp; caching](/triage-pg/concepts/identity-and-caching/)** — every
  artifact is named by a hash over its full input closure (Guix-style), so identical
  inputs skip the build, provenance is queryable, and the estimator's own version
  enters model identity.
- **[The ranking spine](/triage-pg/concepts/problem-types-and-ranking/)** — triage-pg
  is a prioritization system: one spine (score → rank → evaluate) that `problem_type`
  swaps a few steps on. Label columns and `task_framing` follow from it.
- **[The data model](/triage-pg/concepts/the-data-model/)** — where everything lives:
  a database per project plus a registry control plane, append-only predictions, and
  matrices as Parquet on disk/S3 (never in Postgres).

## How the docs are organized

| If you want… | Read |
|---|---|
| the *why* | these **Concepts** pages |
| to *do it* end to end | the **[Tutorials](/triage-pg/tutorials/)** |
| every config key + contract | the **[Configuration reference](/triage-pg/reference/configuration/)** and the rest of **Reference** |
| a specific "why did they do it this way?" | the Architecture Decision Records (`docs/adr/` in the repo) |
| a common error | the **[FAQ](/triage-pg/faq/)** |

The vocabulary these pages use — *Project, Experiment, Run, as_of_date, Cohort,
Matrix, Prediction, Forward score* — is defined once in the repo's `CONTEXT.md`
glossary. This section uses those terms exactly.
