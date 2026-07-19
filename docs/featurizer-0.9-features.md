# featurizer v0.9.x feature families in triage-pg

**Status:** current pin `featurizer[parquet] @ v0.9.1` (`pyproject.toml`, ADR-0008/0016).
**Date:** 2026-07-18.

featurizer v0.9.0/v0.9.1 add the **text and graph feature families** (the taxonomy's former
`[GAP]` substrates) plus one native engine addition. Everything is **additive** — v0.9.1 is
"zero engine change", and v0.9.0's only engine change is the opt-in `graph_relationships` pass —
so the triage-pg seam (ADR-0008) and the scale verdict ([`featurizer-scale.md`](featurizer-scale.md))
carry forward unchanged. This page records what is now available to a triage-pg experiment and
how to reach it without breaking point-in-time correctness.

## What's new in the engine (v0.9.x)

- **Text Path-1 bridges** (`featurizer/bridge/nlp.py`, multilingual, Spanish register by
  default): `SentimentBridge` (lexicon valence), `ReadabilityBridge` (Fernández-Huerta / Flesch),
  `LanguageIdBridge` (categorical), `NERCountsBridge` (spaCy persons/orgs/locations/money/dates).
- **Graph bridges**: `CentralityBridge` (degree/in/out/weighted, coreness, clustering by default;
  betweenness/eigenvector/closeness opt-in via `include_heavy=`) and `CommunityBridge` (Louvain
  membership as a categorical + modularity). Snapshot-aware.
- **Native 1-hop `graph_relationships` pass** (v0.9.0, the one engine change): a top-level config
  block over an edge table (required `timestamp`) that emits `DEGREE(<name>)` (+ one windowed
  variant per interval) and `NEIGHBOUR_MEAN` / `NEIGHBOUR_SHARE` columns in pure SQL, bounded by
  **both** the edge timestamp and the neighbour state's `temporal_ix`. **Strictly 1-hop** — 2-hop
  aggregation (the canonical temporal-GNN leakage) is deliberately not offered.
- **Trajectory / sequence / text-induced edges** (v0.9.1): `EmbeddingTrajectoryBridge`
  (per-event novelty / drift / volatility vs the entity's strictly-prior embeddings),
  `ChangePointBridge` + `PeriodicityBridge` (pre-t₀ measure-series shift / FFT rhythm), and edge
  builders `NearDuplicateEdgeBridge` (MinHash/LSH) + `CoMentionEdgeBridge` that *materialize an
  `(src, dst, ts)` edge table* — exactly what the graph bridges and `graph_relationships` consume.

Full detail lives in featurizer's own docs: the **bridge cookbook**, the **configuration
reference**, and the **CHANGELOG** at `ccd-ia/featurizer` (v0.9.0 / v0.9.1).

## How triage-pg consumes them

triage-pg forwards the experiment config's `feature_config` to featurizer **verbatim** — the
adapter (`adapters/matrix.py:_featurizer_config_yaml`) only does `dict(feature_config)` →
`yaml.safe_dump`, forcing `as_of_boundary: exclusive` (the strictly-before rule) and nothing
else. So the new capabilities are reachable today, two ways:

1. **`graph_relationships` block** — add it as a top-level key inside `feature_config`. It flows
   straight through to featurizer; triage-pg still forces the strictly-before boundary, and the
   pass is 1-hop-bounded on both timestamps by construction. You supply the edge table (either a
   real relationship table, or one materialized by a `*EdgeBridge`).
2. **Bridge families** — a bridge is a *pre-step*: materialize its output as an ordinary event
   table (its `materialize_*` writes `(entity[, as_of_date], columns…)`), then reference that
   table as an `entities` node (+ `relationships` edge) in `feature_config` like any other event
   source. triage-pg's pipeline does **not** orchestrate the materialization for you — run it as a
   Dagster/Snakemake asset (or a one-off) before `triage run`, then point the config at the result.

## Point-in-time correctness (unchanged, still the cardinal rule)

- triage-pg forces `as_of_boundary: exclusive` regardless of what the config requests — features
  for an `as_of_date` use only data knowable **strictly before** it.
- featurizer's bridges are snapshot-aware / strictly-prior by design (ADR-0014 on the featurizer
  side): first events are NULL (no history), models rebuild per as-of window on the pre-t₀ slice,
  and `graph_relationships` excludes both future edges and future neighbour state.
- Keep `knowledge_date_column` semantics: a bridge's `ts` must reflect when the signal *became
  known*, not when the underlying event occurred.

## What did NOT change

- The DFS engine core and its scale characteristics — the
  [`featurizer-scale.md`](featurizer-scale.md) verdict (scalable as-is) carries forward; v0.9.x is
  additive with no DFS-planner change, so the pin bump was **not** re-benchmarked.
- The triage-pg ↔ featurizer seam (ADR-0008): triage owns timechop→`as_of_dates`, cohort, labels,
  matrix assembly, cache keys, and the imputation split (ADR-0009); featurizer owns DFS. Triage
  concepts still must never leak into featurizer.
